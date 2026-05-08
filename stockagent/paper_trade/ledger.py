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

_TIME_STOP_DAYS = 30  # max holding period if neither stop nor target hit
_RSI_EXIT = 60        # mean-reversion exit threshold (matches strategies.py)

# Trailing stop config
_TRAIL_TRIGGER_PCT = 0.05   # activate trailing after position is up >+5%
_TRAIL_ATR_MULT = 1.5       # trailing distance = 1.5 × ATR(14)


def _trailing_stop_for(symbol: str, d: date, current_close: float, current_stop: float) -> float:
    """Return the BETTER of (current_stop, trailing_stop_today). Stops only RATCHET UP.

    Trailing rule: if position is up >5% from entry, set stop = max(current_stop,
    current_close - 1.5 × ATR(14)). Locks in profit while letting winners run.
    """
    end_ts = pd.Timestamp(d)
    start = end_ts - pd.Timedelta(days=80)
    df = load_prices(symbol, start=start.date(), end=d)
    if df.empty:
        return current_stop
    df = df.droplevel("symbol").sort_index()
    from stockagent.indicators.compute import add_indicators
    df = add_indicators(df, ["atr14"])
    if df.empty or pd.isna(df["atr14"].iloc[-1]):
        return current_stop
    atr = float(df["atr14"].iloc[-1])
    if not math.isfinite(atr) or atr <= 0:
        return current_stop
    candidate = current_close - _TRAIL_ATR_MULT * atr
    return max(current_stop, candidate)  # never lower the stop


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
            """SELECT id, decision_id, symbol, qty, entry_price, entry_date, status
               FROM paper_trades WHERE status = 'open'"""
        )).mappings()]


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
    start = end_ts - pd.Timedelta(days=80)
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
    max_picks: int = 5,
) -> int:
    """Deterministic-only decision generator for replay/backfill. NO LLM calls.
    Writes to coordinator_decisions so process_day can fill them next bar.
    Idempotent: if decisions already exist for this date, returns 0."""
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

    # Size positions, respecting locks. We can't fully respect cumulative cash here
    # since process_day handles cash; we only enforce per-trade caps.
    rows = []
    run_id = f"replay-{d.isoformat()}"
    used = 0.0
    for s in sigs[:max_picks * 2]:  # generous over-pick; size limits trim further
        stop_dist = s.entry_price - s.stop_price
        if stop_dist <= 0:
            continue
        qty_by_risk = int((settings.capital_inr * settings.max_risk_per_trade_pct) // stop_dist)
        qty_by_alloc = int((settings.capital_inr * settings.max_allocation_pct) // s.entry_price)
        qty = max(0, min(qty_by_risk, qty_by_alloc))
        if qty == 0:
            continue
        alloc = qty * s.entry_price
        if used + alloc > settings.capital_inr:
            continue
        used += alloc
        target = s.entry_price + 2 * stop_dist
        rows.append({
            "run_id": run_id,
            "symbol": s.symbol,
            "verdict": "bullish",
            "conviction": 0.6,  # deterministic baseline conviction
            "entry": s.entry_price,
            "stop": s.stop_price,
            "target": target,
            "pos": alloc,
            "qty": qty,
            "horizon": 20,
            "rationale": f"deterministic replay: {s.rationale}",
            "created_at": f"{d} 18:00:00",
        })
        if len(rows) >= max_picks:
            break

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
    bars = _bars_for_date(d, [p["symbol"] for p in open_positions])

    fills = exits_stop = exits_signal = exits_time = 0

    # --- Step 1: process exits for already-open positions on bar d ---
    new_open: list[dict] = []
    for pos in open_positions:
        sym = pos["symbol"]
        if sym not in bars:
            new_open.append(pos)
            continue
        bar = bars[sym]
        low = float(bar["low"])
        open_px = float(bar["open"])
        close_px = float(bar["close"])
        entry_date = pd.Timestamp(pos["entry_date"]).date()
        days_held = (d - entry_date).days

        # Pull stop from the originating decision
        with engine.connect() as c:
            stop = c.execute(
                text("SELECT stop_loss FROM coordinator_decisions WHERE id = :id"),
                {"id": pos["decision_id"]},
            ).scalar()
        stop = float(stop) if stop is not None else None

        exit_reason = None
        exit_fill = None
        # Trailing-stop adjustment: if position is up >5%, ratchet stop up.
        if stop is not None and math.isfinite(stop):
            unrealized_pct = (close_px - pos["entry_price"]) / pos["entry_price"]
            if unrealized_pct >= _TRAIL_TRIGGER_PCT:
                new_stop = _trailing_stop_for(sym, d, close_px, stop)
                if new_stop > stop:
                    with engine.begin() as cc:
                        cc.execute(text("UPDATE coordinator_decisions SET stop_loss = :s WHERE id = :id"),
                                   {"s": new_stop, "id": pos["decision_id"]})
                    stop = new_stop
        if stop is not None and math.isfinite(stop) and low <= stop:
            exit_fill = costs.slip_sell(min(open_px, stop))
            exit_reason = "stop"  # could be original stop or trailing stop — both labelled 'stop'
        elif days_held >= _TIME_STOP_DAYS:
            exit_fill = costs.slip_sell(close_px)
            exit_reason = "time"
        elif _signal_exit_today(sym, d):
            # Signal fired yesterday → fill at today's open (no same-bar entry+exit)
            exit_fill = costs.slip_sell(open_px)
            exit_reason = "signal"

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
            if exit_reason == "stop":
                exits_stop += 1
            elif exit_reason == "time":
                exits_time += 1
            else:
                exits_signal += 1
        else:
            new_open.append(pos)

    open_positions = new_open

    # --- Step 2: fill yesterday's coordinator picks at today's open ---
    yesterday = d - timedelta(days=1)
    while yesterday.weekday() >= 5:  # walk back over weekends
        yesterday -= timedelta(days=1)
    decisions = _decisions_made_on(yesterday)
    if decisions:
        sym_set = {dec["symbol"] for dec in decisions}
        bars = {**bars, **_bars_for_date(d, sym_set)}

    held_syms = {p["symbol"] for p in open_positions}
    # Idempotency: skip decisions already filled in a prior process_day run
    with engine.connect() as c:
        already_filled = set(
            c.execute(text("SELECT decision_id FROM paper_trades WHERE decision_id IS NOT NULL")).scalars().all()
        )

    for dec in decisions:
        sym = dec["symbol"]
        if dec["id"] in already_filled:
            continue
        if sym in held_syms:
            continue
        if sym not in bars:
            continue
        bar = bars[sym]
        open_px = float(bar["open"])
        if not math.isfinite(open_px):
            continue
        fill = costs.slip_buy(open_px)
        qty = int(dec["qty"])
        value = qty * fill
        buy_cost = costs.buy_cost(value)
        if cash < value + buy_cost:
            allowable = cash - 1
            qty = int(allowable / fill)
            if qty <= 0:
                continue
            value = qty * fill
            buy_cost = costs.buy_cost(value)
        cash -= value + buy_cost
        with engine.begin() as c:
            c.execute(text(
                """INSERT INTO paper_trades (decision_id, symbol, side, qty, entry_price, entry_date,
                                             brokerage_inr, status)
                   VALUES (:dec_id, :sym, 'BUY', :qty, :px, :d, :bc, 'open')"""
            ), {"dec_id": dec["id"], "sym": sym, "qty": qty, "px": fill, "d": str(d), "bc": buy_cost})
        fills += 1
        held_syms.add(sym)
        open_positions.append({"id": None, "decision_id": dec["id"], "symbol": sym, "qty": qty, "entry_price": fill, "entry_date": d, "status": "open"})

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


def replay_range(start: date, end: date, *, universe: list[str]) -> list[TickResult]:
    """Replay all trading days in [start, end] sequentially. Idempotent.
    Generates deterministic decisions on each day for next-bar fills."""
    days = _trading_days_in_db(start, end)
    out: list[TickResult] = []
    for d in days:
        r = process_day(d, universe=universe)
        out.append(r)
        logger.info(
            f"{d}: fills={r.fills} stops={r.exits_stop} sig={r.exits_signal} "
            f"time={r.exits_time} open={r.open_positions} nav=₹{r.nav:,.0f} "
            f"pnl=₹{r.day_pnl:+,.0f}"
        )
    return out


def reset_paper_state() -> None:
    """Wipe paper_trades + portfolio_state. Use before a fresh replay."""
    engine = get_engine()
    with engine.begin() as c:
        c.execute(text("DELETE FROM paper_trades"))
        c.execute(text("DELETE FROM portfolio_state"))
