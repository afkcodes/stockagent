"""Diagnose why backfill is slow on a VPS.

Measures each component independently:
  1. DNS resolution to NSE hostnames
  2. Raw TCP/HTTPS roundtrip to NSE servers
  3. Single bhav-copy fetch timing (the actual workload)
  4. SQLite insert throughput
  5. CPU / RAM / disk

Run from the project root:
    uv run python scripts/diagnose_vps.py

Output tells you which layer is the bottleneck and what to fix.
"""
from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import tempfile
import time
from datetime import date

import requests

import stockagent  # noqa: F401  — applies nselib HTTP patch


def section(title: str) -> None:
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


# 1. DNS resolution
section("1. DNS resolution")
for host in ["nseindia.com", "www.nseindia.com", "nsearchives.nseindia.com"]:
    t = time.time()
    try:
        ip = socket.gethostbyname(host)
        print(f"  {host:35s} → {ip}  in {(time.time()-t)*1000:>5.0f}ms")
    except Exception as e:
        print(f"  {host:35s} → FAIL: {e}")


# 2. Raw HTTPS to NSE
section("2. Raw TCP/HTTPS to NSE (3 calls each)")
for url in ["https://nseindia.com/", "https://nsearchives.nseindia.com/"]:
    times = []
    for i in range(3):
        t = time.time()
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            times.append(time.time() - t)
        except Exception as e:
            print(f"  {url} attempt {i+1}: FAIL {e}")
    if times:
        avg = sum(times) / len(times) * 1000
        print(f"  {url:50s} avg {avg:>6.0f}ms")


# 3. Single bhav call timing — the actual bottleneck of the backfill
section("3. Single bhav call timing (the actual bottleneck)")
from nselib import capital_market

test_dates = [date(2024, 6, 14), date(2024, 1, 15), date(2023, 6, 15), date(2022, 6, 15)]
for d in test_dates:
    t = time.time()
    try:
        df = capital_market.bhav_copy_with_delivery(trade_date=d.strftime("%d-%m-%Y"))
        n = len(df) if df is not None else 0
        elapsed = time.time() - t
        print(f"  bhav {d}: {n:>5} rows in {elapsed:>5.2f}s")
    except Exception as e:
        elapsed = time.time() - t
        print(f"  bhav {d}: FAIL in {elapsed:>5.2f}s — {str(e)[:80]}")


# 4. SQLite insert speed (catches slow disk)
section("4. SQLite write speed (insert 10k rows + commit)")
tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db").name
try:
    con = sqlite3.connect(tmp)
    con.execute("CREATE TABLE t (id INTEGER, val REAL, txt TEXT)")
    t = time.time()
    con.executemany(
        "INSERT INTO t VALUES (?, ?, ?)",
        [(i, i * 1.5, f"value_{i}") for i in range(10_000)],
    )
    con.commit()
    elapsed = time.time() - t
    print(f"  10k rows + commit: {elapsed*1000:>5.0f}ms ({10_000/elapsed:>6.0f} rows/sec)")
    con.close()
finally:
    os.unlink(tmp)


# 5. System resources
section("5. CPU / Memory / Disk")
print(f"  CPU count:     {os.cpu_count()}")
try:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith(("MemTotal", "MemFree", "MemAvailable", "SwapTotal", "SwapFree")):
                print(f"  {line.strip()}")
except Exception:
    pass
gb_free = shutil.disk_usage(".").free / 1024**3
gb_total = shutil.disk_usage(".").total / 1024**3
print(f"  Disk:          {gb_free:.1f} GB free / {gb_total:.1f} GB total at {os.getcwd()}")


# 6. Quick interpretation
section("Interpretation cheatsheet")
print("""
  - DNS >100ms          → fix VPS resolver (use 1.1.1.1 / 8.8.8.8)
  - HTTPS to NSE >1s    → network throttling or routing issue
  - Single bhav >2s     → NSE per-IP rate limiting (datacenter IPs often slower)
  - SQLite <1000 rows/s → slow disk; consider tmpfs or upgrade VPS plan
  - CPU=1 + low RAM     → cheap VPS instance, swap thrashing possible

  If the bhav call is the smoking gun (most likely):
    a) Let it run overnight in tmux — backfill is one-time
    b) Increase backfill_bhav_range(sleep_sec=1.0) — paradoxically faster
       if NSE is throttling you for being too aggressive
    c) Backfill last 6 months only:
         uv run stockagent backfill-bhav --start $(date -d '6 months ago' +%%Y-%%m-%%d) \\
                                          --end $(date +%%Y-%%m-%%d) --universe all
""")
