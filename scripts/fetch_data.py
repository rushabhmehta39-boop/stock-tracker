"""
Daily stock data fetcher.
Pulls:
  1. BSE result calendar (companies announcing board meetings / results)
  2. NSE equity bhavcopy (close price, volume, delivery %)
  3. NSE F&O data (open interest, PCR) for F&O-eligible stocks
  4. NSE bulk deals and block deals

Saves everything into /data as dated JSON + a rolling latest.json and
a growing history.csv so a website can chart trends over time.

Designed to run once a day via GitHub Actions. If a source fails
(NSE/BSE frequently block bots or change formats), the script logs the
error and continues with whatever it could fetch, rather than crashing
the whole run.
"""

import requests
import pandas as pd
import json
import io
import os
import sys
import time
from datetime import datetime, timedelta

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(DATA_DIR, exist_ok=True)

# IST "today" (GitHub Actions runners use UTC)
IST_OFFSET = timedelta(hours=5, minutes=30)
now_ist = datetime.utcnow() + IST_OFFSET
TODAY_STR = now_ist.strftime("%Y-%m-%d")
TODAY_COMPACT = now_ist.strftime("%d%m%Y")     # DDMMYYYY for NSE bhavcopy urls
TODAY_YMD = now_ist.strftime("%Y%m%d")         # YYYYMMDD

log_lines = []


def log(msg):
    line = f"[{datetime.utcnow().isoformat()}] {msg}"
    print(line)
    log_lines.append(line)


def nse_session():
    """
    NSE blocks plain requests without a browser-like session.
    Standard workaround: hit the homepage first to collect cookies,
    then reuse that session for API calls.
    """
    s = requests.Session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    s.headers.update(headers)
    try:
        s.get("https://www.nseindia.com", timeout=10)
        time.sleep(1)
    except Exception as e:
        log(f"NSE session warm-up failed: {e}")
    return s


# ---------------------------------------------------------------------------
# 1. BSE result calendar (board meetings for results)
# ---------------------------------------------------------------------------
def fetch_bse_result_calendar():
    """
    BSE exposes a JSON endpoint behind their 'Forthcoming Board Meetings' /
    'Result Calendar' page. Endpoint/params can change without notice --
    if this breaks, check https://www.bseindia.com/corporates/Forth_Results.aspx
    in the browser network tab for the current endpoint.
    """
    url = "https://api.bseindia.com/BseIndiaAPI/api/AnnBoardMeeting/w"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bseindia.com/corporates/Forth_Results.aspx",
        "Accept": "application/json",
    }
    params = {
        "scripcode": "",
        "strSearch": "R",   # R = results
        "strType": "F",
        "strFrom": now_ist.strftime("%Y%m%d"),
        "strTo": (now_ist + timedelta(days=14)).strftime("%Y%m%d"),
    }
    try:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "Table" in data:
            rows = data["Table"]
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        log(f"BSE result calendar: {len(rows)} entries fetched")
        return rows
    except Exception as e:
        log(f"BSE result calendar fetch FAILED: {e}")
        return []


# ---------------------------------------------------------------------------
# 2. NSE equity bhavcopy -> price, volume, delivery %
# ---------------------------------------------------------------------------
def fetch_nse_bhavcopy():
    """
    NSE 'security-wise delivery position' full bhavcopy contains
    close price, traded qty, and delivery qty/% for every symbol.
    URL pattern: sec_bhavdata_full_DDMMYYYY.csv
    """
    url = f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{TODAY_COMPACT}.csv"
    s = nse_session()
    try:
        r = s.get(url, timeout=20)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        df.columns = [c.strip() for c in df.columns]
        log(f"NSE bhavcopy: {len(df)} rows fetched")
        return df
    except Exception as e:
        log(f"NSE bhavcopy fetch FAILED (market may be closed / holiday / url format changed): {e}")
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# 3. NSE F&O OI / PCR
# ---------------------------------------------------------------------------
def fetch_nse_fo_oi():
    """
    Pulls per-symbol option chain summary for computing OI build-up and
    Put-Call Ratio. Uses NSE's option-chain-indices/equities JSON API.
    NOTE: this endpoint only works for F&O-eligible symbols; for a full
    market scan we query the master list of F&O symbols first.
    """
    s = nse_session()
    result = {}
    try:
        master_url = "https://archives.nseindia.com/content/fo/fo_mktlots.csv"
        r = s.get(master_url, timeout=15)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        symbol_col = df.columns[1] if len(df.columns) > 1 else df.columns[0]
        symbols = [str(x).strip() for x in df[symbol_col].dropna().unique() if str(x).strip().isupper()]
        log(f"F&O symbol master list: {len(symbols)} symbols")
    except Exception as e:
        log(f"F&O master list fetch FAILED: {e}")
        symbols = []

    # Limit per-run to avoid long execution / rate limiting; sample a batch each day if list is huge
    MAX_SYMBOLS = 60
    for sym in symbols[:MAX_SYMBOLS]:
        try:
            oc_url = f"https://www.nseindia.com/api/option-chain-equities?symbol={sym}"
            r = s.get(oc_url, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
            records = data.get("records", {})
            ce_oi = sum(
                item["CE"]["openInterest"]
                for item in records.get("data", [])
                if "CE" in item and "openInterest" in item["CE"]
            )
            pe_oi = sum(
                item["PE"]["openInterest"]
                for item in records.get("data", [])
                if "PE" in item and "openInterest" in item["PE"]
            )
            pcr = round(pe_oi / ce_oi, 3) if ce_oi else None
            result[sym] = {"call_oi": ce_oi, "put_oi": pe_oi, "pcr": pcr}
            time.sleep(0.5)  # be gentle
        except Exception as e:
            log(f"F&O OI fetch failed for {sym}: {e}")
            continue

    log(f"F&O OI/PCR: fetched for {len(result)} symbols")
    return result


# ---------------------------------------------------------------------------
# 4. Bulk & block deals
# ---------------------------------------------------------------------------
def fetch_bulk_block_deals():
    s = nse_session()
    out = {"bulk": [], "block": []}
    for kind, url in [
        ("bulk", "https://www.nseindia.com/api/historical/bulk-deals"),
        ("block", "https://www.nseindia.com/api/historical/block-deals"),
    ]:
        try:
            r = s.get(url, timeout=15)
            r.raise_for_status()
            data = r.json()
            rows = data.get("data", []) if isinstance(data, dict) else data
            out[kind] = rows
            log(f"{kind.capitalize()} deals: {len(rows)} entries fetched")
        except Exception as e:
            log(f"{kind.capitalize()} deals fetch FAILED: {e}")
    return out


# ---------------------------------------------------------------------------
# Save results
# ---------------------------------------------------------------------------
def save_all(bse_calendar, bhav_df, fo_data, deals):
    day_payload = {
        "date": TODAY_STR,
        "bse_result_calendar": bse_calendar,
        "fo_oi_pcr": fo_data,
        "bulk_deals": deals.get("bulk", []),
        "block_deals": deals.get("block", []),
        "equity_rows": bhav_df.to_dict(orient="records") if not bhav_df.empty else [],
        "fetch_log": log_lines,
    }

    # 1. Dated snapshot
    dated_path = os.path.join(DATA_DIR, f"{TODAY_STR}.json")
    with open(dated_path, "w") as f:
        json.dump(day_payload, f, indent=2, default=str)

    # 2. Rolling "latest" pointer for the website to read
    latest_path = os.path.join(DATA_DIR, "latest.json")
    with open(latest_path, "w") as f:
        json.dump(day_payload, f, indent=2, default=str)

    # 3. Append to a long-running equity history CSV (for trend charts)
    if not bhav_df.empty:
        history_path = os.path.join(DATA_DIR, "equity_history.csv")
        bhav_df["FETCH_DATE"] = TODAY_STR
        if os.path.exists(history_path):
            bhav_df.to_csv(history_path, mode="a", header=False, index=False)
        else:
            bhav_df.to_csv(history_path, mode="w", header=True, index=False)

    log(f"Saved daily snapshot -> {dated_path}")


def main():
    log(f"=== Run started for {TODAY_STR} ===")
    bse_calendar = fetch_bse_result_calendar()
    bhav_df = fetch_nse_bhavcopy()
    fo_data = fetch_nse_fo_oi()
    deals = fetch_bulk_block_deals()
    save_all(bse_calendar, bhav_df, fo_data, deals)
    log("=== Run complete ===")


if __name__ == "__main__":
    main()
