import os
import json
import hmac
import time
import requests
import pandas as pd
import streamlit as st
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import db as persistence
import market_data as marketfeed
import market_monitor

load_dotenv()
IST = ZoneInfo("Asia/Kolkata")
DHAN_BASE = "https://api.dhan.co/v2"

st.set_page_config(page_title="Dhan AI Trading Supervisor", page_icon="📊", layout="wide")

def env(name, default=""):
    return os.getenv(name, default).strip()

def auth_ok():
    expected = env("DASHBOARD_PASSWORD")
    supplied = st.session_state.get("dashboard_password", "")
    if not expected:
        return False
    return hmac.compare_digest(supplied, expected)

def dhan_get(path):
    client_id = env("DHAN_CLIENT_ID")
    token = env("DHAN_ACCESS_TOKEN")
    if not client_id or not token:
        raise RuntimeError("Dhan credentials are not configured in the server environment.")
    # This adapter deliberately exposes GET requests only.
    response = requests.get(
        DHAN_BASE + path,
        headers={"access-token": token, "client-id": client_id, "Content-Type": "application/json"},
        timeout=15,
    )
    response.raise_for_status()
    if not response.content:
        return []
    return response.json()

def as_frame(payload):
    if isinstance(payload, list):
        return pd.DataFrame(payload)
    if isinstance(payload, dict):
        # Some endpoints return a dict wrapper; preserve the payload for visibility.
        for key in ("data", "orders", "trades", "positions"):
            if isinstance(payload.get(key), list):
                return pd.DataFrame(payload[key])
        return pd.DataFrame([payload])
    return pd.DataFrame()

def mask_value(value):
    s = str(value)
    if len(s) <= 4:
        return "****"
    return "*" * (len(s)-4) + s[-4:]

st.title("📊 Dhan AI Trading Supervisor")
st.caption("Phase 1 • Read-only monitoring • No order execution")

if not env("DASHBOARD_PASSWORD"):
    st.error("Security setup required: set DASHBOARD_PASSWORD in the hosting provider's environment settings, then redeploy.")
    st.stop()

if not auth_ok():
    st.subheader("Sign in")
    st.text_input("Dashboard password", type="password", key="dashboard_password")
    if st.button("Unlock dashboard"):
        if auth_ok():
            st.rerun()
        st.error("Incorrect password.")
    st.stop()

client_id = env("DHAN_CLIENT_ID")
if not client_id or not env("DHAN_ACCESS_TOKEN"):
    st.warning("Dhan API credentials are missing. Add DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in your hosting provider's environment settings.")
    st.stop()

st.info("Safety boundary: this project only sends GET requests to read account data. It cannot send trading instructions.")

c1, c2, c3 = st.columns(3)
c1.metric("Broker", "Dhan")
c2.metric("Strategies", "Zen + Curvature")
c3.metric("Mode", "READ ONLY")

with st.sidebar:
    st.header("Monitoring")
    refresh = st.slider("Refresh interval (seconds)", 15, 120, 30, 15)
    st.caption("Manual refresh is recommended during initial validation.")
    show_raw = st.checkbox("Show raw API payloads", value=False)
    st.divider()
    st.write("**Attribution policy**")
    st.caption("No trade is assigned to Zen or Curvature unless reliable strategy metadata or a verified mapping exists. Contract similarity alone is not proof.")
    st.caption("Market quotes are read-only. Non-zero open positions are watched automatically; optional Nifty/reference instruments are configured with MARKET_WATCHLIST_JSON.")

if "refresh_generation" not in st.session_state:
    st.session_state["refresh_generation"] = 0
if "last_persisted_generation" not in st.session_state:
    st.session_state["last_persisted_generation"] = -1
if "last_market_persisted_generation" not in st.session_state:
    st.session_state["last_market_persisted_generation"] = -1
if st.button("Refresh Dhan data", type="primary"):
    st.session_state["refresh_at"] = time.time()
    st.session_state["refresh_generation"] += 1
    st.session_state["manual_refresh_requested"] = True

@st.cache_data(ttl=15, show_spinner=False)
def fetch_data():
    # Read-only GET endpoints documented by DhanHQ.
    return {
        "positions": dhan_get("/positions"),
        "orders": dhan_get("/orders"),
        "trades": dhan_get("/trades"),
    }

@st.cache_data(ttl=45, show_spinner=False)
def fetch_market_monitoring(watchlist_json: str, spreads_json: str, refresh_generation: int):
    # Cache key includes the explicit refresh generation, so routine UI reruns do
    # not generate repeated market-data requests. Endpoint allowlisting is in market_data.py.
    watchlist = json.loads(watchlist_json)
    spread_configs = marketfeed.parse_spread_configs(spreads_json)
    return market_monitor.collect_market_snapshot(watchlist, spread_configs)

if st.session_state.pop("manual_refresh_requested", False):
    fetch_data.clear()
    fetch_market_monitoring.clear()

try:
    with st.spinner("Reading positions, orders and trades from Dhan..."):
        data = fetch_data()
    st.success("Dhan read-only data retrieval succeeded.")
except Exception as e:
    st.error(f"Unable to retrieve Dhan data: {type(e).__name__}: {e}")
    st.warning("Check token validity, API permissions, endpoint availability and network access. This app does not submit or change orders.")
    st.stop()

# Persist once on first successful load, then on explicit manual refreshes.
# Other Streamlit UI reruns do not create duplicate position snapshots.
db_write_counts = None
db_table_counts = None
if env("DATABASE_URL") and st.session_state["last_persisted_generation"] != st.session_state["refresh_generation"]:
    try:
        with st.spinner("Saving this read-only snapshot to PostgreSQL..."):
            db_write_counts = persistence.persist_snapshot(data)
        db_table_counts = persistence.get_table_counts()
        st.session_state["last_persisted_generation"] = st.session_state["refresh_generation"]
    except Exception as db_error:
        # Do not display connection strings or raw exception text, which can contain secrets.
        st.warning(f"Dhan data loaded, but PostgreSQL persistence failed ({type(db_error).__name__}). Check Render logs and DATABASE_URL configuration; credentials are not shown here.")
elif not env("DATABASE_URL"):
    st.warning("PostgreSQL is not configured: add DATABASE_URL in Render Environment to enable persistent history.")

positions = as_frame(data["positions"])
orders = as_frame(data["orders"])
trades = as_frame(data["trades"])

market_watchlist = []
spread_configs = []
market_snapshot = None
market_write_counts = None
market_config_error = None
try:
    market_watchlist, spread_configs = marketfeed.build_watchlist(
        data["positions"],
        env("MARKET_WATCHLIST_JSON"),
        env("MARKET_SPREADS_JSON"),
    )
except ValueError as config_error:
    market_config_error = str(config_error)

if market_watchlist and market_config_error is None:
    try:
        with st.spinner("Fetching read-only market quotes..."):
            market_snapshot = fetch_market_monitoring(
                json.dumps(market_watchlist, sort_keys=True),
                env("MARKET_SPREADS_JSON"),
                st.session_state["refresh_generation"],
            )
        if env("DATABASE_URL") and st.session_state["last_market_persisted_generation"] != st.session_state["refresh_generation"]:
            with st.spinner("Saving market observations to PostgreSQL..."):
                market_write_counts = persistence.persist_market_snapshot(market_snapshot)
            st.session_state["last_market_persisted_generation"] = st.session_state["refresh_generation"]
            db_table_counts = persistence.get_table_counts()
    except Exception as market_error:
        # Do not expose response bodies, request headers, or credentials in the UI.
        st.warning(f"Market monitoring is unavailable for this refresh ({type(market_error).__name__}). Check Dhan Data API access, the configured security IDs, and Render logs.")
elif market_config_error:
    st.warning(f"Market monitoring configuration needs attention: {market_config_error}")

st.subheader("Account snapshot")
a, b, c = st.columns(3)
a.metric("Open position rows", int(len(positions)))
b.metric("Today's order rows", int(len(orders)))
c.metric("Today's trade rows", int(len(trades)))

def numeric_sum(frame, candidates):
    for col in candidates:
        if col in frame.columns:
            vals = pd.to_numeric(frame[col], errors="coerce")
            return float(vals.fillna(0).sum())
    return None

pnl = numeric_sum(positions, ["realizedProfit", "realisedProfit", "realizedPnl", "realizedPnL"])
unreal = numeric_sum(positions, ["unrealizedProfit", "unrealisedProfit", "unrealizedPnl", "unrealizedPnL"])
p1, p2, p3 = st.columns(3)
p1.metric("Reported realised P&L", "—" if pnl is None else f"₹{pnl:,.2f}")
p2.metric("Reported unrealised P&L", "—" if unreal is None else f"₹{unreal:,.2f}")
p3.metric("Attribution status", "UNVERIFIED", help="Strategy attribution requires verified Stratzy metadata or manually validated order mapping.")

st.subheader("Open positions (Dhan source)")
if positions.empty:
    st.info("Dhan returned no open positions in this snapshot.")
else:
    st.dataframe(positions, use_container_width=True, hide_index=True)

st.subheader("Orders (today)")
if orders.empty:
    st.info("No orders returned.")
else:
    st.dataframe(orders, use_container_width=True, hide_index=True)

st.subheader("Trades (today)")
if trades.empty:
    st.info("No trades returned.")
else:
    st.dataframe(trades, use_container_width=True, hide_index=True)

st.subheader("Market Monitoring — read-only")
st.caption("Quotes are observational snapshots from Dhan. Data can be delayed, unavailable, or wide; do not treat these calculations as automated exit instructions.")
if market_config_error:
    st.error(market_config_error)
elif not market_watchlist:
    st.info("No quoteable instruments were found in open positions and MARKET_WATCHLIST_JSON is empty. Add verified instrument IDs in Render Environment to monitor the Nifty index or a traded Nifty futures reference. See the Phase 3 guide.")
elif market_snapshot is None:
    st.info("Market quotes have not been retrieved successfully yet. Click Refresh Dhan data and check the Render logs if the problem continues.")
else:
    quote_rows = market_snapshot.get("quote_rows", [])
    indicator_rows = market_snapshot.get("indicator_rows", [])
    if indicator_rows:
        indicator_frame = pd.DataFrame(indicator_rows)
        visible_columns = [
            ("instrument_name", "Instrument"), ("exchange_segment", "Segment"),
            ("security_id", "Security ID"), ("role", "Role"), ("last_price", "LTP"),
            ("vwap", "VWAP"), ("vwap_position", "LTP vs VWAP"),
            ("current_day_high", "Today High"), ("current_day_low", "Today Low"),
            ("previous_session_high", "Prev High"), ("previous_session_low", "Prev Low"),
            ("recent_12_candle_high", "Recent 12×5m High"), ("recent_12_candle_low", "Recent 12×5m Low"),
            ("volume", "Volume"), ("open_interest", "Open Interest"), ("best_bid", "Best Bid"),
            ("best_ask", "Best Ask"), ("level_status", "Price-level status"),
        ]
        present = [(raw, label) for raw, label in visible_columns if raw in indicator_frame.columns]
        display_frame = indicator_frame[[raw for raw, _ in present]].rename(columns={raw: label for raw, label in present})
        st.dataframe(display_frame, use_container_width=True, hide_index=True)
        st.caption("VWAP prefers Dhan's average_price. When that is not available, it is estimated from configured 5-minute candle typical prices weighted by volume. If no valid volume data exists, VWAP is N/A. Prior-session levels and 5-minute swing ranges require a manually configured reference instrument with fetch_candles=true.")
        st.caption("Quote snapshot captured at (IST): " + str(market_snapshot.get("captured_at", "unknown")))
    else:
        st.warning("Dhan returned no usable market quote rows for this watchlist. Verify IDs and the Data APIs subscription.")

    risk_rows = market_snapshot.get("spread_risk_rows", [])
    st.markdown("**Verified spread-risk estimates**")
    if risk_rows:
        risk_frame = pd.DataFrame(risk_rows)
        display_cols = [c for c in ["spread_name", "strategy_name", "status", "quantity", "entry_credit", "estimated_close_debit", "estimated_pnl", "max_theoretical_loss", "message"] if c in risk_frame.columns]
        st.dataframe(risk_frame[display_cols], use_container_width=True, hide_index=True)
        st.caption("Spread estimates require an explicitly human-verified MARKET_SPREADS_JSON configuration, matched quantities and fresh two-sided quotes. P&L and theoretical loss exclude costs and execution slippage.")
    else:
        st.info("Spread P&L is intentionally not inferred from similar-looking contracts. It remains unavailable until MARKET_SPREADS_JSON contains both verified legs, actual entry fills, matched total units, strike width, and verified=true.")

    if market_snapshot.get("errors"):
        with st.expander("Market-data warnings", expanded=True):
            for issue in market_snapshot["errors"]:
                st.warning(issue)
    if market_write_counts:
        st.caption("Market records saved this refresh: " + ", ".join(f"{key}={value}" for key, value in market_write_counts.items()))
    if show_raw and quote_rows:
        with st.expander("Raw market quote payloads"):
            st.json(quote_rows)

st.subheader("Zen / Curvature attribution")
st.warning("This starter version intentionally does not guess which strategy owns a broker position. Confirm whether Stratzy supplies strategy-specific order tags/correlation IDs before enabling automated attribution.")
attribution = pd.DataFrame([
    {"Strategy": "Zen Credit Spread Overnight", "Attribution": "Pending verification", "Open spread count": "—"},
    {"Strategy": "Curvature Credit Spread Overnight", "Attribution": "Pending verification", "Open spread count": "—"},
])
st.dataframe(attribution, use_container_width=True, hide_index=True)

st.subheader("Data freshness")
st.write("Snapshot time (IST):", datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z"))
st.caption("This version polls Dhan account data with GET and market quote/chart data only through the allowlisted read-only data endpoints. It does not use a WebSocket listener or execute trading actions; market monitoring updates on manual refresh and cached Streamlit reruns.")

st.subheader("Persistent storage")
if db_table_counts is not None:
    st.success("PostgreSQL is connected; the latest successful poll was saved.")
    storage_rows = [{"Table": name, "Stored rows": count} for name, count in db_table_counts.items()]
    st.dataframe(pd.DataFrame(storage_rows), use_container_width=True, hide_index=True)
    if db_write_counts is not None:
        st.caption("Rows observed in this poll: " + ", ".join(f"{k}={v}" for k, v in db_write_counts.items()))
elif env("DATABASE_URL"):
    st.info("PostgreSQL is configured. A successful snapshot save will be confirmed here after the next refresh.")
else:
    st.info("PostgreSQL is not configured; snapshots are not being retained across restarts.")

st.caption("The database stores account history plus market quote, candle, indicator and spread-risk snapshots. Attribution remains UNMATCHED until verified. Telegram delivery and scheduled EOD reporting are not enabled in this increment.")

if show_raw:
    with st.expander("Raw positions payload"):
        st.json(data["positions"])
    with st.expander("Raw orders payload"):
        st.json(data["orders"])
    with st.expander("Raw trades payload"):
        st.json(data["trades"])

st.divider()
st.caption("Never share your Dhan access token in chat, screenshots, GitHub, or Telegram. Rotate it immediately if exposed.")
