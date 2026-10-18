# Dhan AI Trading Supervisor — Phase 3 market monitoring

This increment extends the Phase 2 Streamlit dashboard and PostgreSQL persistence layer with read-only Dhan market monitoring.

## New files
- `market_data.py`: statically allowlisted Dhan market-data/chart requests.
- `market_monitor.py`: indicators, price levels and conservative spread-risk estimates.

## New PostgreSQL tables
- `market_snapshots`
- `market_candles`
- `indicator_snapshots`
- `spread_risk_snapshots`

The eight Phase 2 tables remain in place.

## Render variables
Keep `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN`, `DASHBOARD_PASSWORD`, and `DATABASE_URL`.
Optional configuration:
- `MARKET_WATCHLIST_JSON`: verified JSON array of market instruments. This is needed for index/reference instruments not present in open positions and for candle-derived levels.
- `MARKET_SPREADS_JSON`: optional JSON array of manually verified spread definitions. Leave empty until both legs, actual entry fills, matched total units, strike width, and strategy attribution are confirmed.

See `GITHUB_RENDER_UPDATE_GUIDE.md` for exact setup instructions and safe placeholder examples.

## Read-only boundary
Account data uses GET requests for positions/orders/trades. Market data uses POST only to a fixed allowlist of Dhan quote/chart endpoints. No order placement, modification, cancellation, or automatic exit implementation is included. Do not add trading endpoints to the allowlist.
