# stock-tracker

Daily BSE/NSE stock data tracker — fully automated.

## What this does
Every day at midnight (IST), a GitHub Actions workflow automatically:
1. Fetches the BSE result calendar (companies announcing results in the next 14 days)
2. Fetches NSE's daily equity bhavcopy (close price, volume, delivery %)
3. Fetches F&O open interest and Put-Call Ratio for F&O-eligible stocks
4. Fetches bulk deal and block deal reports
5. Saves everything into `/data` and commits it back to this repo
6. The website (`index.html`, served via GitHub Pages) reads the latest data and flags stocks
   showing signs of pre-positioning (high delivery %, bulk/block deals, extreme PCR)

## Manually triggering a run
Go to the **Actions** tab → **Daily Stock Data Fetch** → **Run workflow**, instead of waiting
for the midnight schedule.

## If a data source breaks
NSE and BSE occasionally change their site structure or add stronger bot protection.
Check `data/latest.json` → `fetch_log` for error messages from the last run, and check
`scripts/fetch_data.py` for the relevant endpoint.
