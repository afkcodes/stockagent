from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import click
from loguru import logger
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from sqlalchemy import text

from stockagent.config import settings
from stockagent.db.session import get_engine, init_db as _init_db

console = Console()

# Default to INFO so backfills show progress; override with LOGURU_LEVEL=DEBUG for chatty.
logger.remove()
logger.add(sys.stderr, level=os.environ.get("LOGURU_LEVEL", "INFO"))


@click.group()
def cli() -> None:
    """stockagent — Indian-markets multi-agent research CLI."""
    pass


@cli.command("init-db")
def init_db_cmd() -> None:
    """Create the SQLite DB with the full schema."""
    path = _init_db()
    console.print(f"[green]DB ready at[/] {path}")
    console.print(
        f"Capital ₹{settings.capital_inr:,.0f} | "
        f"Max alloc/stock ₹{settings.max_allocation_inr:,.0f} ({settings.max_allocation_pct:.0%}) | "
        f"Max risk/trade ₹{settings.max_risk_per_trade_inr:,.0f} ({settings.max_risk_per_trade_pct:.0%})"
    )


@cli.command("backfill")
@click.option("--universe", default="nifty50", type=click.Choice(["nifty50", "nifty500", "custom"]))
@click.option("--years", default=5, type=int)
@click.option("--symbols", default=None, help="Comma-separated symbols (used when universe=custom)")
@click.option("--limit", default=None, type=int, help="Max symbols to process (smoke testing)")
def backfill_cmd(universe: str, years: int, symbols: str | None, limit: int | None) -> None:
    """Backfill OHLCV history into the prices table."""
    from stockagent.data.nse import backfill_symbol, fetch_constituents

    if universe == "custom":
        if not symbols:
            raise click.UsageError("--symbols is required when --universe=custom")
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    elif universe == "nifty50":
        syms = fetch_constituents("NIFTY 50")
    else:
        syms = fetch_constituents("NIFTY 500")

    if limit:
        syms = syms[:limit]

    console.print(f"Backfilling [bold]{len(syms)}[/] symbols, last [bold]{years}y[/]")
    total_rows = 0
    failed: list[str] = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("symbols", total=len(syms))
        for s in syms:
            try:
                n = backfill_symbol(s, years=years)
                total_rows += n
                progress.console.log(f"{s}: {n} rows")
            except Exception as e:
                logger.exception(f"backfill failed for {s}")
                failed.append(s)
                progress.console.log(f"[red]{s} FAILED:[/] {e}")
            progress.advance(task)
    console.print(f"\n[green]Done.[/] {total_rows} total rows. Failures: {len(failed)}")
    if failed:
        console.print(f"Failed symbols: {', '.join(failed)}")


@cli.command("backfill-bhav")
@click.option("--years", default=5, type=int)
@click.option("--start", default=None, help="ISO YYYY-MM-DD; overrides --years")
@click.option("--end", default=None, help="ISO YYYY-MM-DD; defaults to today")
@click.option("--universe", default="all", type=click.Choice(["all", "nifty50", "nifty500"]))
def backfill_bhav_cmd(years: int, start: str | None, end: str | None, universe: str) -> None:
    """Backfill via daily bhavcopies — best for universe-wide multi-year history."""
    from stockagent.data.nse import backfill_bhav_range, fetch_constituents

    end_d = date.fromisoformat(end) if end else date.today()
    start_d = date.fromisoformat(start) if start else end_d - timedelta(days=years * 365)

    sym_filter: set[str] | None = None
    if universe == "nifty50":
        sym_filter = set(fetch_constituents("NIFTY 50"))
        console.print(f"Filtering to Nifty 50 ({len(sym_filter)} symbols)")
    elif universe == "nifty500":
        sym_filter = set(fetch_constituents("NIFTY 500"))
        console.print(f"Filtering to Nifty 500 ({len(sym_filter)} symbols)")
    else:
        console.print("Storing full EQ universe (~2,400 symbols/day)")

    console.print(f"Bhav backfill {start_d} → {end_d} ({(end_d - start_d).days} days)")
    res = backfill_bhav_range(start_d, end_d, symbols=sym_filter)
    console.print(
        f"[green]Done.[/] {res['rows']:,} rows | "
        f"days: success={res['days_success']} skipped(holiday)={res['days_skipped']} "
        f"attempted={res['days_attempted']}"
    )


@cli.command("backfill-yf")
@click.option("--start", required=True, help="ISO YYYY-MM-DD")
@click.option("--end", default=None, help="ISO YYYY-MM-DD; defaults to today")
@click.option("--universe", default="nifty500", type=click.Choice(["nifty50", "nifty500", "custom"]))
@click.option("--symbols", default=None, help="Comma-separated when universe=custom")
@click.option("--exchange", default="NSE", type=click.Choice(["NSE", "BSE"]))
def backfill_yf_cmd(start: str, end: str | None, universe: str, symbols: str | None, exchange: str) -> None:
    """Backfill OHLCV via yfinance — used for pre-2020 history (NSE archive gone)."""
    from stockagent.data.nse import fetch_constituents
    from stockagent.data.yf import backfill_symbols_yf

    end_d = date.fromisoformat(end) if end else date.today()
    start_d = date.fromisoformat(start)

    if universe == "custom":
        if not symbols:
            raise click.UsageError("--symbols required when universe=custom")
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    elif universe == "nifty50":
        syms = fetch_constituents("NIFTY 50")
    else:
        syms = fetch_constituents("NIFTY 500")

    console.print(f"yfinance backfill {len(syms)} symbols ({exchange}) {start_d} → {end_d}")
    res = backfill_symbols_yf(syms, start=start_d, end=end_d, exchange=exchange)
    console.print(
        f"[green]Done.[/] {res['rows']:,} rows | "
        f"done={res['symbols_done']} empty={res['symbols_empty']} failed={res['symbols_failed']}"
    )


@cli.command("backtest")
@click.argument("strategy_name", type=click.Choice(["ema_crossover", "rsi_mean_reversion", "bollinger_breakout",
                                       "ema_crossover_filtered", "rsi_mean_reversion_filtered",
                                       "bollinger_breakout_filtered", "delivery_anomaly"]))
@click.option("--universe", default="nifty50", type=click.Choice(["nifty50", "nifty500", "custom"]))
@click.option("--symbols", default=None, help="Comma-separated when universe=custom")
@click.option("--start", default="2020-01-01")
@click.option("--end", default=None, help="Defaults to today")
def backtest_cmd(strategy_name: str, universe: str, symbols: str | None, start: str, end: str | None) -> None:
    """Run a single backtest and print a summary."""
    from stockagent.backtest.engine import run_backtest
    from stockagent.backtest.metrics import compute_metrics, per_regime_returns
    from stockagent.backtest.strategies import STRATEGIES
    from stockagent.data.nse import fetch_constituents

    end_d = date.fromisoformat(end) if end else date.today()
    start_d = date.fromisoformat(start)
    strategy = STRATEGIES[strategy_name]()

    if universe == "custom":
        if not symbols:
            raise click.UsageError("--symbols required when universe=custom")
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    elif universe == "nifty50":
        syms = fetch_constituents("Nifty 50")
    else:
        syms = fetch_constituents("Nifty 500")

    console.print(f"[bold]Backtest:[/] {strategy.name} | universe={universe} ({len(syms)} symbols) | {start_d} → {end_d}")
    result = run_backtest(strategy, symbols=syms, start=start_d, end=end_d)
    from stockagent.backtest.metrics import persist_run
    m = compute_metrics(result)
    run_id = persist_run(result, m, params={"universe_name": universe})
    console.print(f"[dim]saved run_id={run_id}[/]")
    console.print(
        f"\n[bold]Summary[/]\n"
        f"  Final NAV:     ₹{m.final_nav:>12,.0f}  (start ₹{m.starting_capital:,.0f})\n"
        f"  Total return:  {m.total_return_pct:>7.2f}%   CAGR: {m.cagr_pct:>6.2f}%\n"
        f"  Sharpe:        {m.sharpe:>7.2f}     Sortino: {m.sortino:>6.2f}\n"
        f"  Max DD:        {m.max_drawdown_pct:>7.2f}%\n"
        f"  Trades:        {m.num_trades:>7}    Win rate: {m.win_rate_pct:>5.1f}%\n"
        f"  Avg winner:    {m.avg_winner_pct:>7.2f}%   Avg loser: {m.avg_loser_pct:>6.2f}%\n"
        f"  Profit factor: {m.profit_factor:>7.2f}     Exposure: {m.exposure_pct:>5.1f}%"
    )
    regime_df = per_regime_returns(result)
    if not regime_df.empty:
        console.print("\n[bold]Per-regime[/]")
        for _, r in regime_df.iterrows():
            console.print(
                f"  {r['regime']:18s}  {r['start']}..{r['end']}  "
                f"return={r['ret_pct']:>+7.2f}%  CAGR={r['cagr_pct']:>+7.2f}%"
            )


@cli.command("walkforward")
@click.argument("strategy_name", type=click.Choice(["ema_crossover", "rsi_mean_reversion", "bollinger_breakout",
                                       "ema_crossover_filtered", "rsi_mean_reversion_filtered",
                                       "bollinger_breakout_filtered", "delivery_anomaly"]))
@click.option("--universe", default="nifty50", type=click.Choice(["nifty50", "nifty500", "liquid", "custom"]))
@click.option("--symbols", default=None)
@click.option("--start", default="2020-06-01")
@click.option("--end", default=None)
@click.option("--train-months", default=18, type=int)
@click.option("--test-months", default=6, type=int)
@click.option("--min-turnover-cr", default=2.0, type=float, help="Liquid universe: min avg daily turnover (cr)")
def walkforward_cmd(strategy_name, universe, symbols, start, end, train_months, test_months, min_turnover_cr):
    """Walk-forward validation. Tests each window independently to expose overfitting."""
    from stockagent.backtest.strategies import STRATEGIES
    from stockagent.backtest.walkforward import render_walkforward, run_walkforward
    from stockagent.data.nse import fetch_constituents
    from stockagent.data.universe import liquid_universe

    end_d = date.fromisoformat(end) if end else date.today()
    start_d = date.fromisoformat(start)

    factory = STRATEGIES[strategy_name]
    if universe == "liquid":
        universe_fn = lambda d: liquid_universe(d, min_turnover_cr=min_turnover_cr)
        # Probe size at start of first test window for header
        first_window_start = start_d.replace(day=1)
        sample = universe_fn(start_d)
        console.print(f"[bold]Walk-forward:[/] {strategy_name} | universe=liquid (recomputed/window, sample size {len(sample)} @ {start_d}) | "
                      f"{start_d}→{end_d} | train={train_months}mo test={test_months}mo")
        results = run_walkforward(factory, universe_fn=universe_fn, start=start_d, end=end_d,
                                  train_months=train_months, test_months=test_months)
    else:
        if universe == "custom":
            if not symbols:
                raise click.UsageError("--symbols required when universe=custom")
            syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        elif universe == "nifty50":
            syms = fetch_constituents("Nifty 50")
        else:
            syms = fetch_constituents("Nifty 500")
        console.print(f"[bold]Walk-forward:[/] {strategy_name} | {universe} ({len(syms)} symbols) | "
                      f"{start_d}→{end_d} | train={train_months}mo test={test_months}mo")
        results = run_walkforward(factory, symbols=syms, start=start_d, end=end_d,
                                  train_months=train_months, test_months=test_months)
    render_walkforward(strategy_name, results)


@cli.command("watchlist")
@click.option("--universe", default="nifty500", type=click.Choice(["nifty50", "nifty500"]))
@click.option("--as-of", default=None, help="ISO date; defaults to most recent in DB")
@click.option("--max-picks", default=5, type=int)
@click.option("--min-conviction", default=0.5, type=float)
def watchlist_cmd(universe: str, as_of: str | None, max_picks: int, min_conviction: float) -> None:
    """Generate today's ranked watchlist from viable strategies + LLM agent (or stub)."""
    from stockagent.agents.coordinator import run_coordinator
    from stockagent.data.nse import fetch_constituents
    from stockagent.signals.daily import latest_trading_day_in_db

    as_of_d = date.fromisoformat(as_of) if as_of else latest_trading_day_in_db()
    syms = fetch_constituents("Nifty 50") if universe == "nifty50" else fetch_constituents("Nifty 500")

    console.print(f"[bold]Watchlist:[/] universe={universe} ({len(syms)} symbols) | as_of={as_of_d}")
    picks = run_coordinator(symbols=syms, as_of=as_of_d, max_picks=max_picks, min_conviction=min_conviction)
    if not picks:
        console.print("[dim]No signals fired today.[/]")
        return

    console.print(f"\n[bold]Top {len(picks)} picks[/] (capital ₹{settings.capital_inr:,.0f}, max alloc {settings.max_allocation_pct:.0%}, max risk {settings.max_risk_per_trade_pct:.0%})\n")
    total_alloc = 0.0
    from stockagent.data.market_movers import confluence_flags
    for i, p in enumerate(picks, 1):
        rr = (p.target - p.entry) / (p.entry - p.stop) if p.target and p.entry > p.stop else float("nan")
        total_alloc += p.position_size_inr
        flags = confluence_flags(p.symbol, as_of_d) if as_of_d else []
        flags_line = f"   [yellow]confluence:[/] {', '.join(flags)}\n" if flags else ""
        console.print(
            f"[bold]{i}. {p.symbol}[/]  ({p.strategy})\n"
            f"   entry: ₹{p.entry:>9,.2f}   stop: ₹{p.stop:>9,.2f}   target: ₹{p.target:>9,.2f}   R:R = 1:{rr:.1f}\n"
            f"   qty: {p.qty}    alloc: ₹{p.position_size_inr:>9,.0f}    horizon: {p.horizon_days}d\n"
            f"   verdict: {p.verdict} (conviction {p.conviction:.2f})\n"
            f"{flags_line}"
            f"   rationale: {p.rationale}\n"
        )
    console.print(f"[dim]Total deployed: ₹{total_alloc:,.0f} of ₹{settings.capital_inr:,.0f}  (cash left ₹{settings.capital_inr - total_alloc:,.0f})[/]")


@cli.group("market-movers")
def movers_group() -> None:
    """NSE live-analysis screens: most-active, gainers/losers, volume spikes, circuit hitters."""


@movers_group.command("fetch")
@click.option("--as-of", default=None, help="ISO date label; defaults to today")
def movers_fetch_cmd(as_of: str | None) -> None:
    """Pull all 7 NSE live screens and persist to market_movers table."""
    from stockagent.data.market_movers import fetch_and_persist_all
    d = date.fromisoformat(as_of) if as_of else date.today()
    counts = fetch_and_persist_all(as_of=d)
    console.print(f"[bold]Fetched market movers for {d}[/]")
    for cat, n in counts.items():
        flag = "[green]✓[/]" if n > 0 else "[yellow]∅[/]"
        console.print(f"  {flag} {cat:25s} {n:>3} rows")


@movers_group.command("show")
@click.option("--as-of", default=None)
@click.option("--category", default=None, help="Filter to one category (default: all)")
@click.option("--limit", default=10, type=int)
def movers_show_cmd(as_of: str | None, category: str | None, limit: int) -> None:
    """Show stored market movers."""
    from sqlalchemy import text as _t
    d = date.fromisoformat(as_of) if as_of else date.today()
    engine = get_engine()
    where = "date = :d"
    params: dict = {"d": str(d)}
    if category:
        where += " AND category = :c"
        params["c"] = category
    with engine.connect() as c:
        rows = list(c.execute(_t(
            f"SELECT category, rank, symbol, ltp, pchange, volume, turnover "
            f"FROM market_movers WHERE {where} ORDER BY category, rank LIMIT :lim"
        ), {**params, "lim": limit * 7}).mappings())
    if not rows:
        console.print(f"[dim]No movers for {d}. Run `market-movers fetch` first.[/]")
        return
    last_cat = None
    for r in rows:
        if r["category"] != last_cat:
            console.print(f"\n[bold]{r['category']}[/]")
            last_cat = r["category"]
        if r["rank"] >= limit:
            continue
        ltp = f"₹{r['ltp']:>8.2f}" if r["ltp"] else "      —"
        pch = f"{r['pchange']:+6.2f}%" if r["pchange"] is not None else "    —"
        vol = f"{r['volume']:>13,}" if r["volume"] else "            —"
        console.print(f"  #{r['rank']+1:>2}  {r['symbol']:14s}  {ltp}  {pch}  vol {vol}")


@movers_group.command("discover")
@click.option("--as-of", default=None)
@click.option("--exclude-nifty500/--no-exclude-nifty500", default=False,
              help="Show only stocks NOT in Nifty 500 (true discovery — what we'd otherwise miss)")
@click.option("--limit", default=15, type=int)
def movers_discover_cmd(as_of: str | None, exclude_nifty500: bool, limit: int) -> None:
    """Surface today's notable movers NOT already in our watchlist/holdings.
    Useful for spotting setups beyond the algorithmic picks."""
    from sqlalchemy import text as _t
    from stockagent.data.market_movers import discover_unwatched
    from stockagent.data.nse import fetch_constituents

    d = date.fromisoformat(as_of) if as_of else date.today()
    engine = get_engine()

    held = set()
    with engine.connect() as c:
        held |= {r[0] for r in c.execute(_t("SELECT DISTINCT symbol FROM paper_trades WHERE status='open'"))}
        held |= {r[0] for r in c.execute(_t(
            "SELECT DISTINCT symbol FROM coordinator_decisions WHERE date(created_at) = :d"
        ), {"d": str(d)})}

    universe_filter = None
    if exclude_nifty500:
        n500 = set(fetch_constituents("Nifty 500"))
        held |= n500  # by treating Nifty 500 as "already considered", we surface ONLY non-N500 names
        # universe_filter stays None — we want all symbols not in held∪n500

    df = discover_unwatched(as_of=d, held_or_picked=held, universe_filter=universe_filter, limit=limit)
    if df.empty:
        console.print("[dim]No notable movers outside your watchlist today.[/]")
        return
    console.print(f"\n[bold]Discovery — movers worth a look[/] (as_of {d})\n")
    last_cat = None
    for _, r in df.iterrows():
        if r["category"] != last_cat:
            console.print(f"\n[bold]{r['category']}[/]")
            last_cat = r["category"]
        ltp = f"₹{r['ltp']:>8.2f}" if pd_notna(r["ltp"]) else "      —"
        pch = f"{r['pchange']:+6.2f}%" if pd_notna(r["pchange"]) else "    —"
        vol = f"{int(r['volume']):>13,}" if pd_notna(r["volume"]) else "            —"
        console.print(f"  #{r['rank']+1:>2}  {r['symbol']:14s}  {ltp}  {pch}  vol {vol}")


def pd_notna(v) -> bool:
    import pandas as _pd
    return _pd.notna(v) and v is not None


@cli.command("daily-tick")
@click.option("--universe", default="nifty500", type=click.Choice(["nifty50", "nifty500"]))
@click.option("--max-picks", default=5, type=int)
@click.option("--min-conviction", default=0.3, type=float)
@click.option("--skip-bhav-refresh", is_flag=True, help="Don't pull today's bhav (use existing DB)")
@click.option("--skip-movers", is_flag=True, help="Don't fetch live market movers (use last fetch)")
def daily_tick_cmd(universe: str, max_picks: int, min_conviction: float, skip_bhav_refresh: bool, skip_movers: bool) -> None:
    """End-of-day routine: refresh data → process paper trades → generate tomorrow's watchlist.

    Run this once daily after 15:35 IST. Each step is independent — failures in one
    don't block the others. The output is your action list for tomorrow's open."""
    from datetime import timedelta
    from stockagent.agents.coordinator import run_coordinator
    from stockagent.data.market_movers import fetch_and_persist_all, confluence_flags
    from stockagent.data.nse import backfill_bhav_range, fetch_constituents
    from stockagent.paper_trade.ledger import process_day
    from stockagent.signals.daily import latest_trading_day_in_db

    console.rule(f"[bold]Daily tick — {date.today()}[/]")

    # 1. Refresh bhav: catch up from latest DB date to today
    if not skip_bhav_refresh:
        last = latest_trading_day_in_db()
        target_end = date.today()
        if last is None:
            console.print("[yellow]DB empty — run a full backfill first.[/]")
            return
        if last < target_end:
            start = last + timedelta(days=1)
            console.print(f"[cyan]→ refreshing bhav {start} → {target_end}[/]")
            res = backfill_bhav_range(start, target_end, symbols=None)
            console.print(f"   added {res['rows']:,} rows ({res['days_success']} trading days)")
        else:
            console.print(f"[dim]bhav up to date through {last}[/]")
    else:
        console.print("[dim]bhav refresh skipped[/]")

    # 2. Fetch today's market movers (live screens)
    if not skip_movers:
        console.print(f"[cyan]→ fetching market movers[/]")
        try:
            counts = fetch_and_persist_all(as_of=date.today())
            ok = sum(1 for v in counts.values() if v > 0)
            console.print(f"   {ok}/{len(counts)} categories populated")
        except Exception as e:
            console.print(f"[yellow]   movers fetch hit issue: {e}[/]")
    else:
        console.print("[dim]movers fetch skipped[/]")

    # 3. Paper-tick: process the latest trading day in the ledger
    latest = latest_trading_day_in_db()
    if latest is None:
        console.print("[red]no prices in DB[/]")
        return
    syms_for_decisions = fetch_constituents("Nifty 50") if universe == "nifty50" else fetch_constituents("Nifty 500")
    console.print(f"[cyan]→ paper-tick {latest} (universe {universe})[/]")
    r = process_day(latest, universe=syms_for_decisions)
    console.print(
        f"   fills={r.fills}  exits: stop={r.exits_stop} sig={r.exits_signal} time={r.exits_time}  "
        f"open={r.open_positions}  NAV ₹{r.nav:,.0f}  day P&L ₹{r.day_pnl:+,.0f}"
    )

    # 4. Generate watchlist for next-bar fills
    console.print(f"[cyan]→ generating watchlist for next session[/]")
    picks = run_coordinator(
        symbols=syms_for_decisions, as_of=latest, max_picks=max_picks, min_conviction=min_conviction
    )
    if not picks:
        console.print("[dim]   no qualifying signals.[/]")
    else:
        console.print(f"\n[bold]Tomorrow's watchlist ({len(picks)} picks)[/]\n")
        total_alloc = 0.0
        for i, p in enumerate(picks, 1):
            rr = (p.target - p.entry) / (p.entry - p.stop) if p.target and p.entry > p.stop else float("nan")
            total_alloc += p.position_size_inr
            flags = confluence_flags(p.symbol, latest)
            flags_line = f"   [yellow]confluence:[/] {', '.join(flags)}\n" if flags else ""
            console.print(
                f"[bold]{i}. {p.symbol}[/]  ({p.strategy})\n"
                f"   entry: ₹{p.entry:>9,.2f}   stop: ₹{p.stop:>9,.2f}   target: ₹{p.target:>9,.2f}   R:R = 1:{rr:.1f}\n"
                f"   qty: {p.qty}    alloc: ₹{p.position_size_inr:>9,.0f}    horizon: {p.horizon_days}d\n"
                f"   verdict: {p.verdict} (conviction {p.conviction:.2f})\n"
                f"{flags_line}"
                f"   rationale: {p.rationale}\n"
            )
        console.print(f"[dim]Total deployed: ₹{total_alloc:,.0f} of ₹{settings.capital_inr:,.0f}[/]")

    console.rule("[green]daily tick complete[/]")


@cli.command("paper-replay")
@click.option("--start", required=True, help="ISO YYYY-MM-DD")
@click.option("--end", default=None, help="ISO YYYY-MM-DD; defaults to latest in DB")
@click.option("--universe", default="nifty500", type=click.Choice(["nifty50", "nifty500"]))
@click.option("--reset/--no-reset", default=False, help="Wipe paper_trades + portfolio_state first")
def paper_replay_cmd(start: str, end: str | None, universe: str, reset: bool) -> None:
    """Replay deterministic paper trading over a date range and rebuild portfolio_state."""
    from stockagent.data.nse import fetch_constituents
    from stockagent.paper_trade.ledger import replay_range, reset_paper_state
    from stockagent.signals.daily import latest_trading_day_in_db

    end_d = date.fromisoformat(end) if end else latest_trading_day_in_db()
    start_d = date.fromisoformat(start)
    syms = fetch_constituents("Nifty 50") if universe == "nifty50" else fetch_constituents("Nifty 500")

    if reset:
        reset_paper_state()
        console.print("[yellow]Reset:[/] paper_trades + portfolio_state cleared")

    console.print(f"[bold]Replay:[/] {start_d} → {end_d}  universe={universe} ({len(syms)})")
    results = replay_range(start_d, end_d, universe=syms)
    if not results:
        console.print("[red]No trading days found in DB for that range.[/]")
        return
    final = results[-1]
    starting = settings.capital_inr
    total_ret = (final.nav - starting) / starting * 100
    console.print(
        f"\n[bold]Final[/]  NAV ₹{final.nav:,.0f}  (start ₹{starting:,.0f})  "
        f"return [bold]{total_ret:+.2f}%[/]  open_positions={final.open_positions}\n"
        f"  total fills:  {sum(r.fills for r in results)}\n"
        f"  exits — stop: {sum(r.exits_stop for r in results)}  signal: {sum(r.exits_signal for r in results)}  time: {sum(r.exits_time for r in results)}"
    )


@cli.command("paper-status")
def paper_status_cmd() -> None:
    """Show current paper portfolio state, open positions, and last trades."""
    engine = get_engine()
    with engine.connect() as c:
        ps = c.execute(text("SELECT * FROM portfolio_state ORDER BY date DESC LIMIT 1")).mappings().first()
        opens = list(c.execute(text(
            """SELECT pt.symbol, pt.qty, pt.entry_price, pt.entry_date,
                      cd.stop_loss, cd.target
               FROM paper_trades pt
               LEFT JOIN coordinator_decisions cd ON cd.id = pt.decision_id
               WHERE pt.status = 'open' ORDER BY pt.entry_date"""
        )).mappings())
        recent = list(c.execute(text(
            """SELECT symbol, qty, entry_price, entry_date, exit_price, exit_date, exit_reason,
                      pnl_inr, pnl_pct, status
               FROM paper_trades WHERE status = 'closed'
               ORDER BY exit_date DESC LIMIT 10"""
        )).mappings())

    if ps:
        ret = (ps["nav_inr"] - settings.capital_inr) / settings.capital_inr * 100
        console.print(
            f"[bold]Portfolio @ {ps['date']}[/]\n"
            f"  NAV: ₹{ps['nav_inr']:,.0f}  ({ret:+.2f}% from start)\n"
            f"  Cash: ₹{ps['cash_inr']:,.0f}    Deployed: ₹{ps['deployed_inr']:,.0f}\n"
            f"  Day P&L: ₹{(ps['day_pnl_inr'] or 0):+,.0f}"
        )
    else:
        console.print("[dim]No portfolio state — run paper-replay first.[/]")

    if opens:
        console.print(f"\n[bold]Open positions ({len(opens)})[/]")
        for o in opens:
            console.print(
                f"  {o['symbol']:12s}  qty={o['qty']:>4}  entry ₹{o['entry_price']:>9,.2f} "
                f"on {o['entry_date']}   stop ₹{o['stop_loss']:>9,.2f}   target ₹{o['target']:>9,.2f}"
            )
    if recent:
        console.print(f"\n[bold]Recent closed trades[/]")
        for r in recent:
            console.print(
                f"  {r['symbol']:12s}  {r['entry_date']} → {r['exit_date']}  "
                f"qty={r['qty']:>4}  P&L ₹{r['pnl_inr']:>+8,.0f} ({r['pnl_pct']*100:>+5.2f}%)  ({r['exit_reason']})"
            )


@cli.command("paper-reset")
@click.confirmation_option(prompt="Wipe all paper_trades + portfolio_state?")
def paper_reset_cmd() -> None:
    from stockagent.paper_trade.ledger import reset_paper_state
    reset_paper_state()
    console.print("[yellow]Wiped.[/]")


@cli.command("stats")
def stats_cmd() -> None:
    """Quick DB stats."""
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT COUNT(*) AS n, COUNT(DISTINCT symbol) AS s, MIN(date) AS mn, MAX(date) AS mx FROM prices")).mappings().one()
    console.print(
        f"prices: [bold]{rows['n']:,}[/] rows | [bold]{rows['s']}[/] symbols | "
        f"{rows['mn']} → {rows['mx']}"
    )


if __name__ == "__main__":
    cli()
