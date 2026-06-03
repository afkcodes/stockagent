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
@click.option("--sleep-sec", default=0.2, type=float,
              help="Pause between calls. Higher = friendlier to NSE rate-limits. Try 1.0-2.0 if backfill stalls.")
def backfill_bhav_cmd(years: int, start: str | None, end: str | None, universe: str, sleep_sec: float) -> None:
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

    console.print(f"Bhav backfill {start_d} → {end_d} ({(end_d - start_d).days} days, sleep={sleep_sec}s)")
    res = backfill_bhav_range(start_d, end_d, symbols=sym_filter, sleep_sec=sleep_sec)
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
                                       "bollinger_breakout_filtered", "delivery_anomaly",
                                       "rsi_mean_reversion_mtf"]))
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
                                       "bollinger_breakout_filtered", "delivery_anomaly",
                                       "rsi_mean_reversion_mtf"]))
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
@click.option("--min-conviction", default=0.45, type=float)
@click.option("--skip-bhav-refresh", is_flag=True, help="Don't pull today's bhav (use existing DB)")
@click.option("--skip-movers", is_flag=True, help="Don't fetch live market movers (use last fetch)")
@click.option("--skip-events", is_flag=True, help="Don't refresh the corporate-actions calendar")
@click.option("--no-llm", is_flag=True, help="Skip the LLM agents; use deterministic baseline only")
@click.option("--no-sentiment", is_flag=True, help="Skip the sentiment agent (faster, no news fetch)")
@click.option("--no-telegram", is_flag=True, help="Don't send Telegram summary")
def daily_tick_cmd(universe, max_picks, min_conviction, skip_bhav_refresh, skip_movers,
                   skip_events, no_llm, no_sentiment, no_telegram) -> None:
    """End-of-day routine: refresh data → process paper trades → generate tomorrow's watchlist.

    Run this once daily after 15:35 IST. Each step is independent — failures in one
    don't block the others."""
    from datetime import timedelta
    from stockagent.agents.coordinator import run_coordinator
    from stockagent.alerts.telegram import format_daily_summary, send_telegram, telegram_configured
    from stockagent.data.events import refresh_corporate_actions
    from stockagent.data.market_movers import fetch_and_persist_all, confluence_flags
    from stockagent.data.nse import backfill_bhav_range, fetch_constituents
    from stockagent.data.sectors import get_sector_map, refresh_sector_map
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

    # 2b. Refresh corporate-actions calendar (events to avoid)
    if not skip_events:
        console.print(f"[cyan]→ refreshing corporate-actions calendar[/]")
        try:
            n = refresh_corporate_actions(lookahead_days=60)
            console.print(f"   {n} events stored")
        except Exception as e:
            console.print(f"[yellow]   events fetch hit issue: {e}[/]")

    # 2c. Build/refresh sector map if missing (one-time per ~quarter)
    if not get_sector_map():
        console.print(f"[cyan]→ building sector map (one-time)[/]")
        try:
            m = refresh_sector_map()
            console.print(f"   {len(m)} symbols mapped")
        except Exception as e:
            console.print(f"[yellow]   sector map build hit issue: {e}[/]")

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

    # 4. Generate watchlist for next-bar fills (multi-agent orchestrator)
    console.print(f"[cyan]→ generating watchlist (multi-agent orchestration)[/]")
    picks = run_coordinator(
        symbols=syms_for_decisions, as_of=latest,
        max_picks=max_picks, min_combined_conviction=min_conviction,
        use_llm=not no_llm, with_sentiment=not no_sentiment,
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
            agent_summary = "  ".join(
                f"{a}={v['verdict'][:3]}/{v['conviction']:.2f}" for a, v in p.per_agent.items()
            ) if p.per_agent else ""
            console.print(
                f"[bold]{i}. {p.symbol}[/]  ({p.sector})  [{p.strategy}]\n"
                f"   entry: ₹{p.entry:>9,.2f}   stop: ₹{p.stop:>9,.2f}   target: ₹{p.target:>9,.2f}   R:R = 1:{rr:.1f}\n"
                f"   qty: {p.qty}    alloc: ₹{p.position_size_inr:>9,.0f}    horizon: {p.horizon_days}d\n"
                f"   verdict: {p.final_verdict} (combined conviction {p.conviction:.2f}, macro_mult {p.macro_multiplier:.2f})\n"
                f"   agents: {agent_summary}\n"
                f"{flags_line}"
                f"   rationale: {p.rationale[:600]}\n"
            )
        console.print(f"[dim]Total deployed: ₹{total_alloc:,.0f} of ₹{settings.capital_inr:,.0f}[/]")

    # 5. Telegram alert (image; falls back to text on any failure)
    if not no_telegram and telegram_configured():
        from stockagent.alerts.telegram import send_daily_summary
        console.print(f"[cyan]→ sending Telegram summary (image)[/]")
        if send_daily_summary(
            as_of=latest, nav=r.nav, day_pnl=r.day_pnl, open_count=r.open_positions,
            fills=r.fills,
            exits={"stop": r.exits_stop, "signal": r.exits_signal, "time": r.exits_time},
            picks=picks,
        ):
            console.print("   [green]sent[/]")
        else:
            console.print("   [yellow]send failed (see logs)[/]")
    elif not no_telegram and not telegram_configured():
        console.print("[dim]telegram not configured (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env)[/]")

    console.rule("[green]daily tick complete[/]")


@cli.command("telegram-init")
@click.option("--write-env/--no-write-env", default=True, help="Write TELEGRAM_CHAT_ID into .env automatically")
@click.option("--timeout", default=120, type=int, help="Seconds to wait for a message")
def telegram_init_cmd(write_env: bool, timeout: int) -> None:
    """One-time helper: discovers your Telegram chat_id by waiting for you to message the bot.

    Steps:
      1. Make sure TELEGRAM_BOT_TOKEN is in .env (from BotFather)
      2. Run this command
      3. Open Telegram, find your bot, send any message (e.g. "hi")
      4. The chat_id prints here, and is written to .env

    If getUpdates is consumed by a webhook, this clears it first.
    """
    import time
    import requests
    from pathlib import Path

    if not settings.telegram_bot_token:
        console.print("[red]TELEGRAM_BOT_TOKEN not set in .env[/]")
        console.print("Get one from @BotFather on Telegram, then re-run.")
        return

    base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"

    # 1. Verify bot
    r = requests.get(f"{base}/getMe", timeout=10)
    if r.status_code != 200:
        console.print(f"[red]Bot token invalid:[/] HTTP {r.status_code}: {r.text[:200]}")
        return
    bot_info = r.json().get("result", {})
    bot_name = bot_info.get("username", "?")
    console.print(f"[green]✓[/] Bot reachable: @{bot_name}")

    # 2. Clear any webhook so getUpdates works
    requests.get(f"{base}/deleteWebhook", timeout=10)

    # 3. Drain old updates so we only see fresh messages
    r = requests.get(f"{base}/getUpdates?offset=-1&timeout=0", timeout=10)
    last_id = 0
    if r.status_code == 200:
        results = r.json().get("result", [])
        if results:
            last_id = results[-1]["update_id"]

    # 4. Poll for new messages
    console.print(
        f"\n[yellow]Now open Telegram, find @{bot_name}, send any message (e.g. 'hi').[/]"
    )
    console.print(f"[dim]Waiting up to {timeout}s for your message... (Ctrl+C to abort)[/]\n")

    chat_id: int | None = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(
            f"{base}/getUpdates",
            params={"offset": last_id + 1, "timeout": 25},
            timeout=30,
        )
        if r.status_code != 200:
            console.print(f"[yellow]getUpdates HTTP {r.status_code}; retrying...[/]")
            time.sleep(2)
            continue
        results = r.json().get("result", [])
        for u in results:
            last_id = max(last_id, u["update_id"])
            msg = u.get("message") or u.get("edited_message") or {}
            chat = msg.get("chat") or {}
            cid = chat.get("id")
            if cid:
                chat_id = cid
                first = chat.get("first_name", "")
                console.print(f"[green]✓ Got message from {first}[/]")
                console.print(f"[bold]TELEGRAM_CHAT_ID = {chat_id}[/]")
                break
        if chat_id:
            break

    if not chat_id:
        console.print(f"\n[red]No message received in {timeout}s.[/]")
        console.print("Make sure you sent the message TO the bot (not to yourself).")
        return

    # 5. Optional: send confirmation back
    requests.post(
        f"{base}/sendMessage",
        json={"chat_id": chat_id, "text": "🔧 stockagent telegram-init OK — daily summaries will arrive here."},
        timeout=10,
    )
    console.print("[green]✓[/] confirmation message sent to your chat")

    # 6. Write to .env
    if write_env:
        env_path = Path(".env")
        if not env_path.exists():
            console.print(f"[yellow]No .env in {Path.cwd()} — printed value above; add it manually.[/]")
            return
        lines = env_path.read_text().splitlines()
        new_lines = []
        replaced = False
        for line in lines:
            if line.startswith("TELEGRAM_CHAT_ID="):
                new_lines.append(f"TELEGRAM_CHAT_ID={chat_id}")
                replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            new_lines.append(f"TELEGRAM_CHAT_ID={chat_id}")
        env_path.write_text("\n".join(new_lines) + "\n")
        console.print(f"[green]✓[/] .env updated")


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


@cli.command("symbol-profile")
@click.option("--symbol", default=None, help="Single symbol; omit to show top-N table across all symbols")
@click.option("--top", default=20, type=int, help="Top N symbols by pick count when --symbol omitted")
def symbol_profile_cmd(symbol: str | None, top: int) -> None:
    """The system's track record per symbol — how often we picked it, win rate, P&L."""
    from sqlalchemy import text as _t
    from stockagent.data.sectors import sector_for

    engine = get_engine()
    if symbol:
        sym = symbol.upper()
        with engine.connect() as c:
            decisions = list(c.execute(_t(
                """SELECT id, run_id, conviction, entry, stop_loss, target,
                          position_size_inr, qty, final_verdict, rationale, created_at
                   FROM coordinator_decisions
                   WHERE symbol = :s ORDER BY created_at DESC"""
            ), {"s": sym}).mappings())
            trades = list(c.execute(_t(
                """SELECT pt.id, pt.entry_date, pt.entry_price, pt.qty,
                          pt.exit_date, pt.exit_price, pt.exit_reason,
                          pt.pnl_inr, pt.pnl_pct, pt.status
                   FROM paper_trades pt
                   WHERE pt.symbol = :s ORDER BY pt.entry_date DESC"""
            ), {"s": sym}).mappings())
            agent_rows = list(c.execute(_t(
                """SELECT agent, verdict, conviction, reasoning, created_at, run_id
                   FROM agent_outputs
                   WHERE symbol = :s AND agent != 'orchestrator'
                   ORDER BY created_at DESC LIMIT 30"""
            ), {"s": sym}).mappings())

        if not decisions:
            console.print(f"[dim]No system activity for {sym} yet.[/]")
            return

        sector = sector_for(sym)
        closed = [t for t in trades if t["status"] == "closed"]
        winners = [t for t in closed if (t["pnl_inr"] or 0) > 0]
        win_rate = len(winners) / len(closed) * 100 if closed else 0
        total_pnl = sum((t["pnl_inr"] or 0) for t in closed)

        console.rule(f"[bold]{sym}[/]  ({sector})")
        console.print(
            f"  Picked:     {len(decisions)} times    last on {decisions[0]['created_at']}\n"
            f"  Avg conv:   {sum(d['conviction'] for d in decisions)/len(decisions):.2f}\n"
            f"  Trades:     {len(closed)} closed, {len(trades) - len(closed)} open\n"
            f"  Win rate:   {win_rate:.1f}%   ({len(winners)}/{len(closed)})\n"
            f"  Total P&L:  ₹{total_pnl:>+10,.0f}"
        )

        if trades:
            console.print(f"\n[bold]Trade history[/]")
            for t in trades[:10]:
                if t["status"] == "open":
                    console.print(f"  [yellow]OPEN[/] {t['entry_date']} entry ₹{t['entry_price']:.2f} qty {t['qty']}")
                else:
                    pnl = t["pnl_inr"] or 0
                    color = "green" if pnl > 0 else "red"
                    console.print(
                        f"  {t['entry_date']} → {t['exit_date']}  ₹{t['entry_price']:>8.2f} → ₹{(t['exit_price'] or 0):>8.2f}  "
                        f"qty {t['qty']:>3}  [{color}]₹{pnl:>+8,.0f}[/]  ({(t['pnl_pct'] or 0)*100:>+5.2f}%)  ({t['exit_reason']})"
                    )

        if decisions:
            console.print(f"\n[bold]Most recent decisions[/]")
            for d in decisions[:5]:
                console.print(
                    f"  {d['created_at'][:10]}  {d['final_verdict']:8s}  conv {d['conviction']:.2f}  "
                    f"entry ₹{d['entry']:.2f}  stop ₹{d['stop_loss']:.2f}"
                )
                if d["rationale"]:
                    console.print(f"    [dim]{d['rationale'][:200]}[/]")

        if agent_rows:
            console.print(f"\n[bold]Most recent agent verdicts[/]")
            seen_runs = set()
            for r in agent_rows:
                if r["run_id"] in seen_runs and len([x for x in agent_rows if x["run_id"] == r["run_id"]]) > 4:
                    continue
                seen_runs.add(r["run_id"])
                console.print(
                    f"  {r['created_at'][:10]}  {r['agent']:12s}  {r['verdict']:8s}  "
                    f"conv {r['conviction']:.2f}"
                )
        return

    # Top-N table
    with engine.connect() as c:
        rows = list(c.execute(_t(
            """SELECT cd.symbol,
                      COUNT(DISTINCT cd.id) AS picks,
                      AVG(cd.conviction) AS avg_conv,
                      MAX(cd.created_at) AS last_picked,
                      SUM(CASE WHEN pt.status='closed' THEN 1 ELSE 0 END) AS closed,
                      SUM(CASE WHEN pt.status='open'   THEN 1 ELSE 0 END) AS open_t,
                      SUM(CASE WHEN pt.status='closed' AND pt.pnl_inr > 0 THEN 1 ELSE 0 END) AS winners,
                      SUM(COALESCE(pt.pnl_inr, 0)) AS total_pnl
               FROM coordinator_decisions cd
               LEFT JOIN paper_trades pt ON pt.decision_id = cd.id
               GROUP BY cd.symbol
               ORDER BY picks DESC, total_pnl DESC
               LIMIT :n"""
        ), {"n": top}).mappings())

    if not rows:
        console.print("[dim]No system activity yet. Run daily-tick first.[/]")
        return

    console.rule(f"[bold]Symbol profiles — top {len(rows)} by pick count[/]")
    console.print(
        f"  [dim]{'symbol':14s}  {'sector':18s}  {'picks':>5}  {'avg conv':>8}  "
        f"{'closed':>6}  {'open':>4}  {'win%':>5}  {'total P&L':>12}  {'last picked':>12}[/]"
    )
    for r in rows:
        sym = r["symbol"]
        sector = sector_for(sym)
        wr = (r["winners"] / r["closed"] * 100) if r["closed"] else 0
        pnl = r["total_pnl"] or 0
        color = "green" if pnl > 0 else ("red" if pnl < 0 else "white")
        console.print(
            f"  {sym:14s}  {sector:18s}  {r['picks']:>5}  {(r['avg_conv'] or 0):>8.2f}  "
            f"{r['closed']:>6}  {r['open_t']:>4}  {wr:>4.1f}%  "
            f"[{color}]₹{pnl:>+11,.0f}[/]  {(r['last_picked'] or '')[:10]:>12}"
        )
    console.rule()


@cli.command("paper-summary")
@click.option("--start", default=None, help="ISO YYYY-MM-DD; defaults to first paper trade")
@click.option("--end", default=None, help="ISO YYYY-MM-DD; defaults to today")
def paper_summary_cmd(start: str | None, end: str | None) -> None:
    """End-of-period review: P&L, win rate, per-sector breakdown, drawdown."""
    import statistics
    from sqlalchemy import text as _t

    engine = get_engine()
    with engine.connect() as c:
        # Closed trades in window
        where = ""
        params: dict = {}
        if start:
            where += " AND entry_date >= :s"
            params["s"] = start
        if end:
            where += " AND (exit_date IS NULL OR exit_date <= :e)"
            params["e"] = end
        trades = list(c.execute(_t(
            f"""SELECT pt.symbol, pt.qty, pt.entry_price, pt.entry_date,
                       pt.exit_price, pt.exit_date, pt.exit_reason,
                       pt.pnl_inr, pt.pnl_pct, pt.status
                FROM paper_trades pt
                WHERE 1=1 {where}
                ORDER BY pt.entry_date"""
        ), params).mappings())

        # Portfolio NAV curve
        ps = list(c.execute(_t(
            f"""SELECT date, nav_inr, day_pnl_inr, deployed_inr, cash_inr
                FROM portfolio_state
                WHERE 1=1 {where.replace('entry_date', 'date').replace('exit_date IS NULL OR exit_date', 'date')}
                ORDER BY date"""
        ), params).mappings())

    if not trades and not ps:
        console.print("[dim]No paper-trade activity in this window. Run daily-tick first.[/]")
        return

    closed = [t for t in trades if t["status"] == "closed"]
    open_t = [t for t in trades if t["status"] == "open"]

    # P&L metrics
    pnl_total = sum((t["pnl_inr"] or 0) for t in closed)
    winners = [t for t in closed if (t["pnl_inr"] or 0) > 0]
    losers = [t for t in closed if (t["pnl_inr"] or 0) <= 0]
    win_rate = len(winners) / len(closed) * 100 if closed else 0
    avg_winner = statistics.mean((t["pnl_pct"] or 0) for t in winners) * 100 if winners else 0
    avg_loser = statistics.mean((t["pnl_pct"] or 0) for t in losers) * 100 if losers else 0
    gross_win = sum((t["pnl_inr"] or 0) for t in winners)
    gross_loss = abs(sum((t["pnl_inr"] or 0) for t in losers))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf") if gross_win > 0 else 0

    # NAV curve metrics
    final_nav = float(ps[-1]["nav_inr"]) if ps else settings.capital_inr
    starting = settings.capital_inr
    total_ret_pct = (final_nav - starting) / starting * 100
    if ps:
        navs = [float(r["nav_inr"]) for r in ps]
        peak = navs[0]
        max_dd = 0
        for n in navs:
            peak = max(peak, n)
            dd = (n - peak) / peak * 100
            max_dd = min(max_dd, dd)
    else:
        max_dd = 0

    # Per-sector breakdown via sector_for
    from stockagent.data.sectors import sector_for
    sector_pnl: dict[str, float] = {}
    sector_count: dict[str, int] = {}
    for t in closed:
        s = sector_for(t["symbol"])
        sector_pnl[s] = sector_pnl.get(s, 0) + (t["pnl_inr"] or 0)
        sector_count[s] = sector_count.get(s, 0) + 1

    # Best/worst trades
    best = sorted(closed, key=lambda t: t["pnl_inr"] or 0, reverse=True)[:3]
    worst = sorted(closed, key=lambda t: t["pnl_inr"] or 0)[:3]

    console.rule(f"[bold]Paper-trade summary[/]")
    console.print(f"Window:           {ps[0]['date'] if ps else '—'} → {ps[-1]['date'] if ps else '—'}  ({len(ps)} trading days)\n")
    console.print(f"Starting capital: ₹{starting:>12,.0f}")
    console.print(f"Final NAV:        ₹{final_nav:>12,.0f}  ({total_ret_pct:+.2f}%)")
    console.print(f"Realized P&L:     ₹{pnl_total:>+12,.0f}  ({len(closed)} closed trades)")
    console.print(f"Open positions:   {len(open_t)}")
    console.print(f"Max drawdown:     {max_dd:+.2f}%\n")

    console.print(f"[bold]Trade stats[/]")
    console.print(f"  Win rate:        {win_rate:.1f}%   ({len(winners)} W / {len(losers)} L)")
    console.print(f"  Avg winner:      {avg_winner:+.2f}%   gross ₹{gross_win:,.0f}")
    console.print(f"  Avg loser:       {avg_loser:+.2f}%   gross ₹{gross_loss:,.0f}")
    console.print(f"  Profit factor:   {pf:.2f}\n")

    if sector_pnl:
        console.print(f"[bold]Per-sector P&L[/]")
        for s, pnl in sorted(sector_pnl.items(), key=lambda x: x[1], reverse=True):
            n = sector_count[s]
            console.print(f"  {s:25s}  {n:>3} trades   ₹{pnl:>+12,.0f}")
        console.print()

    if best:
        console.print(f"[bold]Best 3 trades[/]")
        for t in best:
            console.print(f"  {t['symbol']:14s}  {t['entry_date']} → {t['exit_date']}  ₹{t['pnl_inr']:>+8,.0f}  ({(t['pnl_pct'] or 0)*100:>+5.2f}%)  {t['exit_reason']}")
    if worst:
        console.print(f"\n[bold]Worst 3 trades[/]")
        for t in worst:
            console.print(f"  {t['symbol']:14s}  {t['entry_date']} → {t['exit_date']}  ₹{t['pnl_inr']:>+8,.0f}  ({(t['pnl_pct'] or 0)*100:>+5.2f}%)  {t['exit_reason']}")

    console.rule()


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


@cli.group("learn")
def learn_cli() -> None:
    """Auto-learning / evidence-backed feedback loop (see docs/autolearn_design.md)."""
    pass


@learn_cli.command("backfill")
@click.option("--source", default="live", type=click.Choice(["live", "backtest"]),
              help="Label these trades in trade_reviews.")
@click.option("--limit", default=None, type=int, help="Max trades (smoke testing).")
def learn_backfill_cmd(source: str, limit: int | None) -> None:
    """Snapshot every closed trade into trade_reviews (idempotent upsert)."""
    from stockagent.db.session import run_migrations
    from stockagent.learn.capture import backfill_reviews

    applied = run_migrations()
    if applied:
        console.print(f"[yellow]migrations applied:[/] {', '.join(applied)}")
    n = backfill_reviews(source=source, limit=limit)
    console.print(f"[green]recorded {n} trade_reviews[/] (source={source})")


@learn_cli.command("report")
@click.option("--limit", default=15, type=int, help="Rows to show.")
def learn_report_cmd(limit: int) -> None:
    """Summarize the trade_reviews corpus (R-multiple, win rate, alpha vs beta)."""
    engine = get_engine()
    with engine.connect() as conn:
        agg = conn.execute(text(
            """SELECT COUNT(*) n,
                      AVG(CASE WHEN is_win=1 THEN 1.0 ELSE 0.0 END) win_rate,
                      AVG(r_multiple) avg_r, SUM(pnl_inr) pnl,
                      AVG(excess_ret_pct) avg_excess,
                      AVG(mae_pct) avg_mae, AVG(mfe_pct) avg_mfe
               FROM trade_reviews"""
        )).mappings().one()
        if not agg["n"]:
            console.print("[yellow]no trade_reviews yet — run `stockagent learn backfill`[/]")
            return
        console.rule("trade_reviews corpus")
        console.print(
            f"trades: [bold]{agg['n']}[/] | win rate [bold]{(agg['win_rate'] or 0)*100:.1f}%[/] | "
            f"avg R [bold]{agg['avg_r'] or 0:+.2f}[/] | total ₹{agg['pnl'] or 0:+,.0f}"
        )
        console.print(
            f"avg excess vs index [bold]{agg['avg_excess'] or 0:+.2f}%[/] | "
            f"avg MAE {agg['avg_mae'] or 0:+.2f}% | avg MFE {agg['avg_mfe'] or 0:+.2f}%"
        )

        console.rule("by exit reason")
        rows = conn.execute(text(
            """SELECT exit_reason, COUNT(*) n,
                      AVG(CASE WHEN is_win=1 THEN 1.0 ELSE 0.0 END) win_rate,
                      AVG(r_multiple) avg_r, SUM(pnl_inr) pnl
               FROM trade_reviews GROUP BY exit_reason ORDER BY n DESC"""
        )).mappings().all()
        for r in rows:
            console.print(
                f"  {r['exit_reason'] or '?':8s}  n={r['n']:<4d}  win {(r['win_rate'] or 0)*100:>5.1f}%  "
                f"avg R {r['avg_r'] or 0:>+5.2f}  ₹{r['pnl'] or 0:>+10,.0f}"
            )

        console.rule(f"worst {limit} by R-multiple")
        rows = conn.execute(text(
            """SELECT symbol, entry_date, exit_date, r_multiple, pnl_inr,
                      excess_ret_pct, exit_reason
               FROM trade_reviews WHERE r_multiple IS NOT NULL
               ORDER BY r_multiple ASC LIMIT :lim"""
        ), {"lim": limit}).mappings().all()
        for r in rows:
            console.print(
                f"  {r['symbol']:14s} {r['entry_date']}→{r['exit_date']}  "
                f"R {r['r_multiple']:>+5.2f}  ₹{r['pnl_inr']:>+8,.0f}  "
                f"excess {r['excess_ret_pct'] if r['excess_ret_pct'] is not None else 0:>+5.1f}%  {r['exit_reason']}"
            )
        console.rule()


@learn_cli.command("mine")
@click.option("--window-days", default=None, type=int,
              help="Only pool trades closed within N days (default: config; 0 = all).")
@click.option("--source", "sources", multiple=True, type=click.Choice(["live", "backtest"]),
              help="Restrict mining to these sources (repeatable; default: all).")
def learn_mine_cmd(window_days: int | None, sources: tuple[str, ...]) -> None:
    """Recompute agent_reliability + learned_patterns from the reviews corpus."""
    from stockagent.db.session import run_migrations
    from stockagent.learn.mine import recompute_all

    applied = run_migrations()
    if applied:
        console.print(f"[yellow]migrations applied:[/] {', '.join(applied)}")
    res = recompute_all(window_days=window_days, sources=list(sources) or None)
    console.print(
        f"[green]mined[/] {res['learned_patterns']} pattern buckets, "
        f"{res['agent_reliability']} agent-reliability rows"
    )


@learn_cli.command("patterns")
@click.option("--all", "show_all", is_flag=True, help="Show inactive buckets too.")
@click.option("--limit", default=40, type=int, help="Max rows.")
def learn_patterns_cmd(show_all: bool, limit: int) -> None:
    """List mined patterns (active first) and agent reliability."""
    engine = get_engine()
    with engine.connect() as conn:
        where = "" if show_all else "WHERE is_active = 1"
        rows = conn.execute(text(
            f"""SELECT pattern_key, n, win_rate, avg_r, profit_factor, wilson_lb,
                       conviction_mult, size_mult, is_active
                FROM learned_patterns {where}
                ORDER BY is_active DESC, n DESC LIMIT :lim"""
        ), {"lim": limit}).mappings().all()
        if not rows:
            console.print("[yellow]no patterns — run `stockagent learn mine`[/]")
            return
        console.rule("learned patterns" + ("" if show_all else " (active)"))
        for r in rows:
            flag = "[green]●[/]" if r["is_active"] else "[dim]○[/]"
            console.print(
                f"  {flag} {r['pattern_key']:22s} n={r['n']:<4d} win {r['win_rate']*100:>5.1f}% "
                f"avgR {r['avg_r'] if r['avg_r'] is not None else 'na':>6} "
                f"PF {r['profit_factor'] if r['profit_factor'] is not None else 'na':>5} "
                f"wLB {r['wilson_lb']:.2f}  conv×{r['conviction_mult']:.2f} size×{r['size_mult']:.2f}"
            )

        rel = conn.execute(text(
            """SELECT agent, condition, n, win_rate, avg_r, wilson_lb
               FROM agent_reliability ORDER BY agent, n DESC"""
        )).mappings().all()
        if rel:
            console.rule("agent reliability")
            for r in rel:
                console.print(
                    f"  {r['agent']:12s} {r['condition']:16s} n={r['n']:<4d} "
                    f"win {r['win_rate']*100:>5.1f}% avgR {r['avg_r'] if r['avg_r'] is not None else 'na':>6} "
                    f"wLB {r['wilson_lb']:.2f}"
                )
        console.rule()


@learn_cli.command("shadow")
@click.option("--limit", default=30, type=int, help="Max rows.")
@click.option("--matched-only", is_flag=True, help="Only rows where a pattern matched.")
def learn_shadow_cmd(limit: int, matched_only: bool) -> None:
    """Inspect the decision_adjustments audit log (what learning would do)."""
    import json as _json
    engine = get_engine()
    with engine.connect() as conn:
        agg = conn.execute(text(
            """SELECT COUNT(*) n,
                      SUM(CASE WHEN matched_patterns NOT IN ('[]','') AND matched_patterns IS NOT NULL
                               THEN 1 ELSE 0 END) matched,
                      SUM(CASE WHEN shadow=1 THEN 1 ELSE 0 END) shadow
               FROM decision_adjustments"""
        )).mappings().one()
        if not agg["n"]:
            console.print("[yellow]no adjustments logged yet — run a coordinator pass[/]")
            return
        console.rule("decision_adjustments")
        console.print(
            f"logged: [bold]{agg['n']}[/] | with a matched pattern [bold]{agg['matched'] or 0}[/] | "
            f"shadow (not applied) [bold]{agg['shadow'] or 0}[/]"
        )
        where = ""
        if matched_only:
            where = "WHERE matched_patterns NOT IN ('[]','') AND matched_patterns IS NOT NULL"
        rows = conn.execute(text(
            f"""SELECT symbol, base_conviction, adj_conviction, conviction_mult,
                       size_mult, matched_patterns, shadow, created_at
                FROM decision_adjustments {where}
                ORDER BY created_at DESC, id DESC LIMIT :lim"""
        ), {"lim": limit}).mappings().all()
        for r in rows:
            try:
                keys = ", ".join(m["pattern_key"] for m in _json.loads(r["matched_patterns"] or "[]"))
            except (ValueError, TypeError):
                keys = ""
            tag = "[dim]shadow[/]" if r["shadow"] else "[green]LIVE[/]"
            console.print(
                f"  {r['symbol']:14s} {r['base_conviction']:.2f}→{r['adj_conviction']:.2f} "
                f"conv×{r['conviction_mult']:.2f} size×{r['size_mult']:.2f} {tag}  "
                f"[dim]{keys}[/]"
            )
        console.rule()


if __name__ == "__main__":
    cli()
