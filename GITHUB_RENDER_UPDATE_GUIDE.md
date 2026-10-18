# Phase 3 — Market monitoring: GitHub and Render guide

This update extends the existing Phase 2 Dhan AI Trading Supervisor. Keep your existing repository and Render service. Do not create a new project.

## What is included

- `market_data.py`: Dhan read-only market quote and chart adapter.
- `market_monitor.py`: VWAP where valid, OHLC/levels, recent candle ranges, bid/ask and conservative spread estimates.
- `db.py`: retains the eight Phase 2 tables and adds `market_snapshots`, `market_candles`, `indicator_snapshots`, and `spread_risk_snapshots`.
- `app.py`: adds a Market Monitoring dashboard section.
- `tests/test_readonly_boundary.py`: checks the account adapter uses GET only and market-data POST calls use a static allowlist.
- `requirements.txt`: includes the PostgreSQL driver used by Phase 2.

## Critical before upload

This package is built against the Phase 2 starter package in this conversation. If you changed your own `app.py` or `db.py` after Phase 2, do not blindly overwrite your changes; compare and merge them first.

The dashboard can monitor current open position legs automatically when Dhan returns a supported exchange segment and security ID. To monitor an underlying index/reference and calculate candle-derived levels, configure a verified watchlist in Render as described below. No security ID is guessed.

## A. Create a branch in GitHub (recommended)

1. Open your existing private GitHub repository.
2. Open the branch selector (usually shows `main`).
3. Type `phase3-market-monitoring` and choose the option to create the branch from your current deployment branch.
4. Confirm the new branch is selected before editing/uploading files.

If Render deploys only from `main`, changes on this branch will not reach the live service until you merge it. This lets you review the changes first.

## B. Upload the code

1. Download and extract `dhan_ai_supervisor_phase3_update.zip`.
2. In the existing repository, select **Add file → Upload files**.
3. Upload the package files at the repository root: `app.py`, `db.py`, `market_data.py`, `market_monitor.py`, `requirements.txt`, `README.md`, `Procfile`, `.gitignore`, `.env.example`, and the `tests` folder. The new files must remain at the root; only the test file belongs in `tests/`.
4. Do not upload a real `.env` file or any credentials.
5. Commit as `Add read-only market monitoring`.

If GitHub's upload screen will not replace a file, open the existing file, use its edit (pencil) action, paste the matching updated file contents, and commit it. Do not replace unrelated files in your repository.

## C. Configure Render environment variables

Keep existing variables: `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN`, `DASHBOARD_PASSWORD`, and `DATABASE_URL`.

Add `MARKET_WATCHLIST_JSON` as a JSON array of instruments you have verified against Dhan's current instrument list. Example structure only — replace the placeholder IDs and do not copy them as real IDs:

```json
[
  {
    "name": "Verified Nifty futures reference",
    "exchange_segment": "NSE_FNO",
    "security_id": "REPLACE_WITH_VERIFIED_NUMERIC_SECURITY_ID",
    "instrument_type": "FUTIDX",
    "role": "vwap_reference",
    "fetch_candles": true
  }
]
```

Important:
- The example is a shape, not a valid instrument mapping. Replace the ID and confirm the correct `instrument_type` and exchange segment from Dhan's instrument master before saving.
- If you only want to monitor instruments already returned in open positions, you can leave `MARKET_WATCHLIST_JSON` empty. However, an index reference and candle-derived levels will not appear unless a valid reference instrument is configured.
- `fetch_candles: true` requests 5-minute and daily historical candles for that instrument. The app limits candle history requests to the first three configured references per refresh.
- `IDX_I` may return an index quote, but VWAP is deliberately N/A for an index if no meaningful volume-weighted price is available. Use a valid traded futures reference if you need a volume-based reference.

`MARKET_SPREADS_JSON` is optional and should remain empty until you have manually verified every leg, exact entry fill price, total units, strike width, and strategy identity. Do not enable a spread by guessing from similar contracts. Example structure only:

```json
[
  {
    "name": "Human-verified spread label",
    "strategy_name": "Zen Credit Spread Overnight",
    "verified": false,
    "quantity": 65,
    "strike_width": 400,
    "short_leg": {
      "name": "Verified short option",
      "exchange_segment": "NSE_FNO",
      "security_id": "REPLACE_WITH_VERIFIED_SHORT_SECURITY_ID",
      "instrument_type": "OPTIDX",
      "entry_price": 100.0
    },
    "long_leg": {
      "name": "Verified long option",
      "exchange_segment": "NSE_FNO",
      "security_id": "REPLACE_WITH_VERIFIED_LONG_SECURITY_ID",
      "instrument_type": "OPTIDX",
      "entry_price": 20.0
    }
  }
]
```

Do not set `"verified": true` until you have confirmed both exact contracts, actual entry fills, quantity, strike width, and strategy mapping. `quantity` means matched total units, not lot count. Prices and strike width must be in the same quote units. The sample numbers are placeholders only.

To configure these variables:
1. Render Dashboard → your existing web service → **Environment**.
2. Add the key and JSON value.
3. Save and deploy. Keep JSON on one line if the Render editor is easier to use that way.
4. Never put real IDs/credentials in GitHub if your repository or watchlist configuration should be private. The watchlist may contain contract identifiers, but credentials must remain only in Render environment variables.

## D. Deploy

1. If Render auto-deploys from the branch you changed, wait for the deployment to finish. Otherwise, merge to the configured deployment branch only after reviewing the diff, then use **Manual Deploy → Deploy latest commit**.
2. Confirm the build command remains `pip install -r requirements.txt`.
3. Confirm the start command remains `streamlit run app.py --server.address 0.0.0.0 --server.port $PORT` (or your existing working start command).
4. Confirm `DATABASE_URL` and the Dhan environment variables are still configured. Never share their values.

## E. Validate

1. Open the dashboard and sign in.
2. Click **Refresh Dhan data**.
3. Find **Market Monitoring — read-only**.
4. Check quote timestamps, instrument IDs, LTP, bid/ask, volume, VWAP status and level status.
5. Verify at least one quote against Dhan's own app. Confirm the ID and segment match the actual contract.
6. Confirm the new table counts appear in PostgreSQL. The twelve tables should include the original eight plus four market tables.
7. Restart/redeploy and confirm old snapshots remain.
8. Run the safety tests locally if you have Python:
   ```bash
   pip install -r requirements.txt
   python -m pytest -q
   ```

## Safety and limitations

- Account endpoints in `app.py` use GET only: positions, orders and trades.
- `market_data.py` uses POST only for a fixed allowlist of Dhan market quote/chart data endpoints. POST is used by Dhan for some data retrieval APIs; the HTTP method alone does not mean an order is placed.
- No order placement, modification, cancellation or automatic exit endpoint is included.
- Spread risk stays unavailable unless a human explicitly verifies the configuration. Strategy attribution is not inferred from contract, side, quantity, or timestamp alone.
- Market quotes can be stale or illiquid. The displayed P&L is indicative and excludes costs, slippage and execution effects. This is a shadow monitor, not trading advice or an automated exit engine.
- Do not infer index VWAP from index spot values without volume data. The dashboard shows N/A when valid volume-weighted data is unavailable.
