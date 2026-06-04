"""Paper-trade ledger.

Daily cycle for trading day D:
  1. Apply yesterday's coordinator picks: fill BUY at D's open with slippage + costs
  2. For every open position at start of D, check D's bar:
       - if low <= stop  => sell at min(open, stop) with slippage + costs
       - else if exit signal generated yesterday => sell at D's open
       - else if max-horizon reached => sell at D's close (time stop)
  3. Mark portfolio to D's close → write portfolio_state row
  4. Run today's coordinator → tomorrow's pending entries

Idempotent: re-processing a day re-checks open positions but won't double-buy
(uniqueness on `(symbol, status='open')` enforced at insert time).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

import pandas as pd
from loguru import logger
from sqlalchemy import text

from stockagent.backtest.costs import CostModel
from stockagent.config import settings
from stockagent.data.loader import load_prices
from stockagent.db.session import get_engine

_RSI_EXIT = 60        # mean-reversion exit threshold (matches strategies.py)

# NOTE: the trailing stop and 30-day time stop were removed — they were not part
# of the validated strategy and cut winners short (see process_day exits). Re-add
# only after validating them through the backtest engine.


@dataclass
class TickResult:
    date: date
    fills: int
    exits_stop: int
    exits_signal: int
    exits_time: int
    open_positions: int
    nav: float
    cash: float
    deployed: float
    day_pnl: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _last_processed_date() -> date | None:
    engine = get_engine()
    with engine.connect() as c:
        v = c.execute(text("SELECT MAX(date) FROM portfolio_state")).scalar()
    return pd.Timestamp(v).date() if v else None


def _trading_days_in_db(start: date, end: date) -> list[date]:
    engine = get_engine()
    with engine.connect() as c:
        rows = c.execute(
            text("SELECT DISTINCT date FROM prices WHERE date BETWEEN :s AND :e ORDER BY date"),
            {"s": str(start), "e": str(end)},
        ).scalars().all()
    return [pd.Timestamp(r).date() for r in rows]


def _bars_for_date(d: date, symbols: Iterable[str] | None = None) -> dict[str, dict]:
    """Return dict[symbol -> bar dict] for a single trade date."""
    where = "date = :d"
    params: dict = {"d": str(d)}
    if symbols is not None:
        syms = list(symbols)
        keys = [f":s{i}" for i in range(len(syms))]
        where += f" AND symbol IN ({', '.join(keys)})"
        for i, s in enumerate(syms):
            params[f"s{i}"] = s
    engine = get_engine()
    with engine.connect() as c:
        rows = list(c.execute(
            text(f"SELECT symbol, open, high, low, close, volume FROM prices WHERE {where}"),
            params,
        ).mappings())
    return {r["symbol"]: dict(r) for r in rows}


def _open_positions_today() -> list[dict]:
    engine = get_engine()
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(
            """SELECT id, decision_id, symbol, qty, entry_price, entry_date,
                      initial_stop AS stop, status
               FROM paper_trades WHERE status = 'open'"""
        )).mappings()]


def _size_at_fill(entry_price: float, stop_price: float | None, cash_available: float) -> int:
    """Integer qty honouring 5% risk-per-trade, 20% allocation, and available cash.
    Identical to backtest.engine._size_position so the ledger and the validated
    backtest size positions the same way. Returns 0 if not sizeable."""
    if (not entry_price or entry_price <= 0 or stop_price is None
            or not math.isfinite(stop_price) or stop_price >= entry_price):
        return 0
    stop_dist = entry_price - stop_price
    if stop_dist <= 0:
        return 0
    cap = settings.capital_inr
    qty_by_risk = int((cap * settings.max_risk_per_trade_pct) // stop_dist)
    qty_by_alloc = int((cap * settings.max_allocation_pct) // entry_price)
    qty_by_cash = int(cash_available // entry_price)
    return max(0, min(qty_by_risk, qty_by_alloc, qty_by_cash))


def _decisions_made_on(d: date) -> list[dict]:
    """Coordinator picks generated on the EOD of date `d`. They get filled at the next open."""
    engine = get_engine()
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(
            """SELECT id, run_id, symbol, entry, stop_loss, target, qty, position_size_inr,
                      horizon_days, conviction, rationale
               FROM coordinator_decisions
               WHERE date(created_at) = :d
               ORDER BY conviction DESC"""
        ), {"d": str(d)}).mappings()]


def _current_cash_and_positions() -> tuple[float, list[dict]]:
    """Compute current cash by replaying buys/sells against starting capital,
    or by reading the latest portfolio_state row if it exists."""
    engine = get_engine()
    with engine.connect() as c:
        row = c.execute(
            text("SELECT cash_inr FROM portfolio_state ORDER BY date DESC LIMIT 1")
        ).mappings().first()
        cash = float(row["cash_inr"]) if row else float(settings.capital_inr)
    positions = _open_positions_today()
    return cash, positions


def _persist_portfolio_state(d: date, cash: float, deployed: float, nav: float, day_pnl: float, open_positions: list[dict]) -> None:
    import json
    engine = get_engine()
    payload = json.dumps([{"symbol": p["symbol"], "qty": p["qty"], "entry": p["entry_price"]} for p in open_positions])
    sql = text(
        """INSERT INTO portfolio_state (date, cash_inr, deployed_inr, open_positions_json, nav_inr, day_pnl_inr)
           VALUES (:d, :cash, :dep, :pos, :nav, :pnl)
           ON CONFLICT(date) DO UPDATE SET
             cash_inr=excluded.cash_inr, deployed_inr=excluded.deployed_inr,
             open_positions_json=excluded.open_positions_json, nav_inr=excluded.nav_inr,
             day_pnl_inr=excluded.day_pnl_inr"""
    )
    with engine.begin() as c:
        c.execute(sql, {"d": str(d), "cash": cash, "dep": deployed, "pos": payload, "nav": nav, "pnl": day_pnl})


# ---------------------------------------------------------------------------
# Core: process one trading day
# ---------------------------------------------------------------------------

def _signal_exit_today(symbol: str, d: date) -> bool:
    """Exit logic that mirrors RsiMeanReversion exactly: exit when RSI(14) > 60.
    Computed against the prior trading day so we exit at today's open, not today's close
    (avoids same-bar entry+exit when RSI rebounds fast)."""
    end_ts = pd.Timestamp(d)
    # Ample warmup (~270 trading days) so RSI(14) is stable and CONSISTENT with the
    # entry-side computation. An 80-day window seeded RSI differently and produced
    # phantom RSI>60 readings that flushed positions in ~2 days — the core ledger bug.
    start = end_ts - pd.Timedelta(days=400)
    # We want indicator values as of the bar BEFORE today, so the exit fires at today's open.
    df = load_prices(symbol, start=start.date(), end=(end_ts - pd.Timedelta(days=1)).date())
    if df.empty:
        return False
    df = df.droplevel("symbol").sort_index()
    from stockagent.indicators.compute import add_indicators
    df = add_indicators(df, ["rsi14"])
    last = df.iloc[-1]
    rsi = float(last.get("rsi14", float("nan")))
    return math.isfinite(rsi) and rsi > _RSI_EXIT


def generate_decisions_for_day(
    d: date,
    symbols: list[str],
    *,
    max_picks: int | None = None,
) -> int:
    """Deterministic-only decision generator for replay/backfill. NO LLM calls.
    Writes ONE decision per entry signal (like the backtest engine — take all
    signals; process_day cash-limits at fill). process_day re-sizes at fill, so
    the qty stored here is only a reference. Idempotent per date.

    `max_picks` optionally caps the number of decisions (None = all signals)."""
    engine = get_engine()
    with engine.connect() as c:
        existing = c.execute(text(
            "SELECT COUNT(*) FROM coordinator_decisions WHERE date(created_at) = :d"
        ), {"d": str(d)}).scalar()
    if existing:
        return 0

    from stockagent.signals.daily import generate_signals
    sigs = generate_signals(symbols=symbols, as_of=d)
    if not sigs:
        return 0

    # Deterministic, stable order (by symbol) so cash-limited fills are reproducible.
    sigs = sorted(sigs, key=lambda s: s.symbol)
    if max_picks is not None:
        sigs = sigs[:max_picks]

    rows = []
    run_id = f"replay-{d.isoformat()}"
    for s in sigs:
        stop_dist = s.entry_price - s.stop_price
        if stop_dist <= 0:
            continue
        # Reference qty (process_day re-sizes against live cash at fill).
        qty = _size_at_fill(s.entry_price, s.stop_price, settings.capital_inr)
        if qty == 0:
            continue
        target = s.entry_price + 2 * stop_dist
        rows.append({
            "run_id": run_id,
            "symbol": s.symbol,
            "verdict": "bullish",
            "conviction": 0.6,  # deterministic baseline conviction
            "entry": s.entry_price,
            "stop": s.stop_price,
            "target": target,
            "pos": qty * s.entry_price,
            "qty": qty,
            "horizon": 20,
            "rationale": f"deterministic replay: {s.rationale}",
            "created_at": f"{d} 18:00:00",
        })

    if not rows:
        return 0
    sql = text(
        """INSERT INTO coordinator_decisions
              (run_id, symbol, final_verdict, conviction, entry, stop_loss, target,
               position_size_inr, qty, horizon_days, rationale, created_at)
           VALUES
              (:run_id, :symbol, :verdict, :conviction, :entry, :stop, :target,
               :pos, :qty, :horizon, :rationale, :created_at)"""
    )
    with engine.begin() as c:
        c.execute(sql, rows)
    return len(rows)


def process_day(d: date, *, costs: CostModel | None = None, generate_today: bool = True, universe: list[str] | None = None) -> TickResult:
    """Process trading day `d`. Idempotent via UPSERT on portfolio_state and
    skip-if-already-filled guard on entries. If `generate_today`, also creates
    coordinator_decisions for `d` (fills next bar)."""
    costs = costs or CostModel()
    engine = get_engine()

    # Prior NAV (yesterday's close) for day_pnl
    prior_nav = None
    with engine.connect() as c:
        v = c.execute(
            text("SELECT nav_inr FROM portfolio_state WHERE date < :d ORDER BY date DESC LIMIT 1"),
            {"d": str(d)},
        ).scalar()
        prior_nav = float(v) if v is not None else float(settings.capital_inr)

    cash, open_positions = _current_cash_and_positions()

    # Yesterday's coordinator picks = today's pending entries (fill at today's open).
    yesterday = d - timedelta(days=1)
    while yesterday.weekday() >= 5:  # walk back over weekends
        yesterday -= timedelta(days=1)
    decisions = _decisions_made_on(yesterday)

    sym_universe = {p["symbol"] for p in open_positions} | {dec["symbol"] for dec in decisions}
    bars = _bars_for_date(d, sym_universe)

    fills = exits_stop = exits_signal = exits_time = 0
    held_syms = {p["symbol"] for p in open_positions}
    with engine.connect() as c:
        already_filled = set(
            c.execute(text("SELECT decision_id FROM paper_trades WHERE decision_id IS NOT NULL")).scalars().all()
        )

    # --- Step 1: fill pending entries at today's OPEN (mirrors backtest engine) ---
    # Highest-conviction first (live), symbol-stable for ties (replay determinism).
    # Size with min(risk, alloc, cash) AT FILL; one per symbol; skip if cash short.
    for dec in sorted(decisions, key=lambda x: (-(x.get("conviction") or 0.0), x["symbol"])):
        sym = dec["symbol"]
        if dec["id"] in already_filled or sym in held_syms or sym not in bars:
            continue
        open_px = float(bars[sym]["open"])
        if not math.isfinite(open_px):
            continue
        fill = costs.slip_buy(open_px)
        stop = dec.get("stop_loss")
        qty = _size_at_fill(fill, stop, cash)
        if qty <= 0:
            continue
        value = qty * fill
        buy_cost = costs.buy_cost(value)
        if cash < value + buy_cost:
            continue  # engine skips rather than partial-filling
        cash -= value + buy_cost
        with engine.begin() as c:
            res = c.execute(text(
                """INSERT INTO paper_trades (decision_id, symbol, side, qty, entry_price, entry_date,
                                             brokerage_inr, initial_stop, status)
                   VALUES (:dec_id, :sym, 'BUY', :qty, :px, :d, :bc, :istop, 'open')"""
            ), {"dec_id": dec["id"], "sym": sym, "qty": qty, "px": fill, "d": str(d),
                "bc": buy_cost, "istop": stop})
            new_id = res.lastrowid
        fills += 1
        held_syms.add(sym)
        open_positions.append({"id": new_id, "decision_id": dec["id"], "symbol": sym,
                               "qty": qty, "entry_price": fill, "entry_date": d,
                               "stop": stop, "status": "open"})

    # --- Step 2: exits on bar d — signal exit at open, then intraday stop ---
    # Mirrors the validated backtest engine: FIXED stop + strategy signal exit only
    # (no trailing, no time stop). A position entered TODAY can stop out same bar
    # but cannot signal-exit (it had no prior-day signal).
    new_open: list[dict] = []
    for pos in open_positions:
        sym = pos["symbol"]
        if sym not in bars:
            new_open.append(pos)
            continue
        low = float(bars[sym]["low"])
        open_px = float(bars[sym]["open"])
        stop = pos.get("stop")
        stop = float(stop) if stop is not None else None
        entered_before_today = pd.Timestamp(pos["entry_date"]).date() < d

        exit_reason = exit_fill = None
        if entered_before_today and _signal_exit_today(sym, d):
            exit_fill = costs.slip_sell(open_px)
            exit_reason = "signal"
        elif stop is not None and math.isfinite(stop) and low <= stop:
            exit_fill = costs.slip_sell(min(open_px, stop))
            exit_reason = "stop"

        if exit_reason:
            value = pos["qty"] * exit_fill
            sell_cost = costs.sell_cost(value)
            cash += value - sell_cost
            pnl = (exit_fill - pos["entry_price"]) * pos["qty"] - sell_cost
            pnl_pct = (exit_fill - pos["entry_price"]) / pos["entry_price"]
            with engine.begin() as c:
                c.execute(text(
                    """UPDATE paper_trades
                       SET exit_price=:px, exit_date=:d, exit_reason=:r,
                           pnl_inr=:pnl, pnl_pct=:pct, status='closed', brokerage_inr = COALESCE(brokerage_inr,0) + :sc
                       WHERE id=:id"""
                ), {"px": exit_fill, "d": str(d), "r": exit_reason, "pnl": pnl, "pct": pnl_pct, "sc": sell_cost, "id": pos["id"]})
            try:
                from stockagent.learn.capture import record_trade_review
                record_trade_review(pos["id"], source="live")
            except Exception as e:
                logger.warning(f"trade_review capture failed for trade {pos['id']}: {e}")
            if exit_reason == "stop":
                exits_stop += 1
            else:
                exits_signal += 1
        else:
            new_open.append(pos)

    open_positions = new_open

    # --- Step 3: mark to market with today's close ---
    if open_positions:
        bars_all = _bars_for_date(d, [p["symbol"] for p in open_positions])
    else:
        bars_all = {}
    deployed = 0.0
    for p in open_positions:
        b = bars_all.get(p["symbol"])
        if b and pd.notna(b["close"]):
            deployed += p["qty"] * float(b["close"])
    nav = cash + deployed
    day_pnl = nav - prior_nav

    _persist_portfolio_state(d, cash=cash, deployed=deployed, nav=nav, day_pnl=day_pnl, open_positions=open_positions)

    # Step 4: generate today's signals → decisions for tomorrow
    if generate_today and universe:
        try:
            generate_decisions_for_day(d, universe)
        except Exception as e:
            logger.warning(f"generate_decisions_for_day {d} failed: {e}")

    return TickResult(
        date=d,
        fills=fills,
        exits_stop=exits_stop,
        exits_signal=exits_signal,
        exits_time=exits_time,
        open_positions=len(open_positions),
        nav=nav,
        cash=cash,
        deployed=deployed,
        day_pnl=day_pnl,
    )


def _persist_backtest_result(res, *, source: str = "replay") -> None:
    """Write a BacktestResult's trades + daily equity into paper_trades and
    portfolio_state, so paper-status / learn backfill see the same numbers the
    engine produced."""
    engine = get_engine()
    trade_rows = []
    for t in res.trades:
        closed = t.exit_price is not None
        trade_rows.append({
            "symbol": t.symbol, "qty": t.qty,
            "entry_price": float(t.entry_price),
            "entry_date": str(pd.Timestamp(t.entry_date).date()),
            "exit_price": float(t.exit_price) if closed else None,
            "exit_date": str(pd.Timestamp(t.exit_date).date()) if closed and t.exit_date is not None else None,
            "exit_reason": t.exit_reason, "istop": float(t.stop) if t.stop is not None else None,
            "pnl_inr": float(t.pnl_inr) if closed else None,
            "pnl_pct": float(t.pnl_pct) if closed else None,
            "bc": float(t.costs_inr), "status": "closed" if closed else "open",
        })
    state_rows = []
    prev_nav = float(res.starting_capital)
    for d, row in res.equity_curve.iterrows():
        nav = float(row["nav"])
        state_rows.append({
            "d": str(pd.Timestamp(d).date()), "cash": float(row["cash"]),
            "dep": float(row["mtm"]), "pos": "[]", "nav": nav, "pnl": nav - prev_nav,
        })
        prev_nav = nav

    with engine.begin() as c:
        if trade_rows:
            c.execute(text(
                """INSERT INTO paper_trades
                       (symbol, side, qty, entry_price, entry_date, exit_price, exit_date,
                        exit_reason, initial_stop, pnl_inr, pnl_pct, brokerage_inr, status)
                   VALUES (:symbol,'BUY',:qty,:entry_price,:entry_date,:exit_price,:exit_date,
                           :exit_reason,:istop,:pnl_inr,:pnl_pct,:bc,:status)"""
            ), trade_rows)
        if state_rows:
            c.execute(text(
                """INSERT INTO portfolio_state
                       (date, cash_inr, deployed_inr, open_positions_json, nav_inr, day_pnl_inr)
                   VALUES (:d,:cash,:dep,:pos,:nav,:pnl)
                   ON CONFLICT(date) DO UPDATE SET
                     cash_inr=excluded.cash_inr, deployed_inr=excluded.deployed_inr,
                     nav_inr=excluded.nav_inr, day_pnl_inr=excluded.day_pnl_inr"""
            ), state_rows)


def replay_range(start: date, end: date, *, universe: list[str], strategy=None):
    """Replay [start, end] through the VALIDATED backtest engine and persist the
    result. The engine — not the live process_day fill loop — is the source of
    truth: a cash-limited day-by-day re-implementation path-diverges from it, so
    replay uses the engine directly. process_day remains the LIVE execution path.

    Returns the BacktestResult."""
    from stockagent.backtest.engine import run_backtest
    from stockagent.backtest.strategies import RsiMeanReversion

    strategy = strategy or RsiMeanReversion()
    res = run_backtest(strategy, symbols=universe, start=start, end=end)
    _persist_backtest_result(res)
    closed = [t for t in res.trades if t.exit_price is not None]
    wins = sum(1 for t in closed if t.pnl_inr > 0)
    ret = (res.final_nav - res.starting_capital) / res.starting_capital * 100
    logger.info(
        f"replay {start}..{end}: {len(closed)} trades, "
        f"win {wins/len(closed)*100:.1f}% nav ₹{res.final_nav:,.0f} ({ret:+.2f}%)"
        if closed else f"replay {start}..{end}: no trades"
    )
    return res


def reset_paper_state() -> None:
    """Wipe paper_trades + portfolio_state. Use before a fresh replay."""
    engine = get_engine()
    with engine.begin() as c:
        c.execute(text("DELETE FROM paper_trades"))
        c.execute(text("DELETE FROM portfolio_state"))
