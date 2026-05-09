"""Find the sleep_sec that maximizes throughput on a throttled VPS.

Tests three sleep values back-to-back on small windows of trading days,
measuring per-call latency and total wall time. The "winner" is whichever
finishes the same window of N days fastest in wall time.

Counter-intuitive result is common: sleep_sec=1.5 may finish 30 days FASTER
than sleep_sec=0.2 because aggressive callers get throttled to 5+ sec per
call while polite callers stay near baseline.

Run from project root:
    uv run python scripts/probe_throttling.py
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import stockagent  # noqa — patches nselib

from stockagent.data.nse import backfill_bhav_range
from stockagent.db.session import get_engine
from sqlalchemy import text


# Use a window safely far in the past so it's idempotent and won't conflict
# with any "current" rows we've already loaded.
TEST_DAYS = 5  # trading days per probe — small window for quick iteration
SLEEP_VALUES = [0.2, 0.7, 1.5]  # the three sleep_sec settings to compare


def baseline_rowcount() -> int:
    with get_engine().connect() as c:
        return c.execute(text("SELECT COUNT(*) FROM prices")).scalar() or 0


def main() -> None:
    print(f"Probing throttling — {TEST_DAYS} trading days per setting")
    print(f"Testing sleep_sec = {SLEEP_VALUES}")
    print(f"Note: each probe writes to the DB but UPSERTs, so re-runs are idempotent")
    print()

    # Pick three NON-OVERLAPPING windows so we don't get cache effects.
    # Windows ~6 months apart, well in the past.
    windows = [
        (date(2025, 11, 1), date(2025, 11, 1) + timedelta(days=int(TEST_DAYS * 1.5))),
        (date(2025, 6, 1),  date(2025, 6, 1)  + timedelta(days=int(TEST_DAYS * 1.5))),
        (date(2024, 11, 1), date(2024, 11, 1) + timedelta(days=int(TEST_DAYS * 1.5))),
    ]

    results = []
    for sleep_sec, (start, end) in zip(SLEEP_VALUES, windows):
        before_rows = baseline_rowcount()
        print(f"=== sleep_sec={sleep_sec}  window {start} → {end} ===")
        t0 = time.time()
        res = backfill_bhav_range(start, end, symbols=None, sleep_sec=sleep_sec)
        elapsed = time.time() - t0
        added = baseline_rowcount() - before_rows
        per_day = elapsed / max(res["days_success"], 1)
        results.append({
            "sleep_sec": sleep_sec,
            "wall_sec": elapsed,
            "days_success": res["days_success"],
            "rows_added": added,
            "per_day_sec": per_day,
        })
        print(f"  → {res['days_success']} days success, {added:,} rows added")
        print(f"  → wall {elapsed:.1f}s  ({per_day:.2f}s per trading day)")
        print()

    print("=" * 60)
    print("RESULTS — fastest = lowest per_day_sec")
    print("=" * 60)
    results.sort(key=lambda r: r["per_day_sec"])
    for i, r in enumerate(results, 1):
        marker = "← WINNER" if i == 1 else ""
        print(f"  sleep_sec={r['sleep_sec']:>4.1f}  per_day {r['per_day_sec']:>5.2f}s  "
              f"wall {r['wall_sec']:>6.1f}s  {marker}")

    winner = results[0]
    print()
    print("Recommendation:")
    print(f"  Use --sleep-sec {winner['sleep_sec']} for your VPS backfills")
    print(f"  Estimated time for full 6-year backfill (~1565 days):")
    print(f"  {1565 * winner['per_day_sec'] / 60:.1f} minutes")


if __name__ == "__main__":
    main()
