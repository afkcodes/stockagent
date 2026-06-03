#!/usr/bin/env bash
#
# clean_start.sh — pull the latest code, upgrade the DB, wipe the paper ledger
# and the learning corpus for a fresh start, and bring price history up to today.
#
# SAFE BY DESIGN: this never deletes price history. `prices` is a separate,
# insert-only table; only the paper ledger (paper_trades, portfolio_state) and
# the 5 learning tables are wiped. Run it on the VPS to start the live
# forward-test clean.
#
# Usage:
#   ./scripts/clean_start.sh            # interactive (one confirmation)
#   ./scripts/clean_start.sh --yes      # no prompt (for automation)
#   ./scripts/clean_start.sh --skip-pull  # don't git pull (e.g. local dev copy)
#
set -euo pipefail

ASSUME_YES=0
SKIP_PULL=0
for arg in "$@"; do
  case "$arg" in
    --yes) ASSUME_YES=1 ;;
    --skip-pull) SKIP_PULL=1 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

# Run from the repo root regardless of where the script is invoked.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "stockagent clean start — repo: $ROOT"
echo
echo "This will:"
[ "$SKIP_PULL" -eq 0 ] && echo "  • git pull + uv sync (update code/deps)"
echo "  • init-db            (create learning tables + initial_stop; no data dropped)"
echo "  • paper-reset        (WIPE paper_trades + portfolio_state)"
echo "  • learn reset        (WIPE the 5 learning tables)"
echo "  • backfill-bhav      (fill price history up to today — insert-only)"
echo
echo "PRICE HISTORY IS PRESERVED. Only the ledger + learning corpus are wiped."
echo

if [ "$ASSUME_YES" -ne 1 ]; then
  read -r -p "Proceed? [y/N] " reply
  case "$reply" in
    y|Y|yes|YES) ;;
    *) echo "aborted."; exit 1 ;;
  esac
fi

run() { echo; echo "▶ $*"; "$@"; }

if [ "$SKIP_PULL" -eq 0 ]; then
  run git pull --ff-only
  run uv sync
fi

# 1. Schema/migrations — idempotent, never drops data.
run uv run stockagent init-db

# 2. Clean start — wipe ledger + learning corpus (prices untouched).
run uv run stockagent paper-reset --yes
run uv run stockagent learn reset --yes

# 3. Bring prices current. Backfill only the gap: from the latest stored bar to
#    today (re-fetching the last day is an idempotent no-op). Full backfill if empty.
LAST="$(uv run python -c "from sqlalchemy import text; from stockagent.db.session import get_engine; print(get_engine().connect().execute(text('SELECT MAX(date) FROM prices')).scalar() or '')" 2>/dev/null || true)"
if [ -n "$LAST" ]; then
  echo; echo "latest stored price bar: $LAST — backfilling gap to today"
  run uv run stockagent backfill-bhav --start "$LAST"
else
  echo; echo "no prices found — running full backfill"
  run uv run stockagent backfill-bhav
fi

# 4. Verify.
run uv run stockagent stats

echo
echo "✅ clean start complete. Ledger + learning corpus are empty; prices are current."
echo "   cron will run daily-tick on the fresh state, and new trades will capture automatically."
