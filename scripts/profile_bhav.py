"""Time the bhav pipeline end-to-end. Run with: uv run python scripts/profile_bhav.py"""
import time
from datetime import date

import stockagent  # ensures nselib_patch is applied
from stockagent.db.session import init_db
from stockagent.data.nse import (
    HolidayError,
    _normalize_bhav,
    fetch_bhav,
    fetch_constituents,
    upsert_prices,
)
from nselib import capital_market

t0 = time.time()
init_db()
print(f"init_db: {time.time()-t0:.2f}s")

t0 = time.time()
syms = set(fetch_constituents("NIFTY 50"))
print(f"fetch_constituents NIFTY 50: {len(syms)} symbols in {time.time()-t0:.2f}s")

dates = [date(2026, 5, 6), date(2026, 5, 5), date(2026, 5, 4), date(2026, 4, 30), date(2026, 4, 29)]
for d in dates:
    print(f"\n--- {d} ---")
    t0 = time.time()
    raw = capital_market.bhav_copy_with_delivery(trade_date=d.strftime("%d-%m-%Y"))
    t1 = time.time()
    print(f"  raw fetch:    {t1-t0:.2f}s ({len(raw)} rows)")
    df = _normalize_bhav(raw, series=("EQ",))
    t2 = time.time()
    print(f"  normalize:    {t2-t1:.2f}s ({len(df)} EQ rows)")
    df_filt = df[df["symbol"].isin(syms)]
    t3 = time.time()
    print(f"  isin filter:  {t3-t2:.2f}s ({len(df_filt)} rows)")
    n = upsert_prices(df_filt, exchange="NSE", source="nselib_bhav")
    t4 = time.time()
    print(f"  upsert:       {t4-t3:.2f}s ({n} rows)")
    print(f"  TOTAL:        {t4-t0:.2f}s")
