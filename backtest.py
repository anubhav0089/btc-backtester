#!/usr/bin/env python3
"""
BTC strategy backtester.

Tests classic trading strategies on daily BTC/USDT history and compares
each one with simply buying and holding. Paper only: it never trades.

Honest by design:
  * No look-ahead. A signal uses data up to day t's close and is executed
    at day t+1's open.
  * Costs on every trade: exchange fee + slippage (both adjustable).
  * Overfitting check (--optimize): tune a strategy on the first part of
    history, then test the winning settings on years it has never seen.

Usage:
  pip install -r requirements.txt
  python3 backtest.py                    # download data (once), run all strategies
  python3 backtest.py --start 2021-01-01 # only test from this date
  python3 backtest.py --optimize         # overfitting experiment
  python3 backtest.py --fee 0.0005       # cheaper fees
  python3 backtest.py --demo             # synthetic prices, no internet
"""

import argparse
import csv
import math
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone

DATA_DIR = "data"
DATA_FILE = os.path.join(DATA_DIR, "btc_usdt_1d.csv")
DAYS_PER_YEAR = 365   # crypto trades every day

# ------------------------------------------------------------------- data ---


@dataclass
class Bars:
    date: list = field(default_factory=list)   # "YYYY-MM-DD"
    open: list = field(default_factory=list)
    high: list = field(default_factory=list)
    low: list = field(default_factory=list)
    close: list = field(default_factory=list)

    def __len__(self):
        return len(self.close)

    def slice(self, i, j):
        return Bars(self.date[i:j], self.open[i:j], self.high[i:j],
                    self.low[i:j], self.close[i:j])

    def index_of(self, day):
        for i, d in enumerate(self.date):
            if d >= day:
                return i
        return len(self.date)


def download(path=DATA_FILE):
    """Fetch all daily BTC/USDT candles from a public exchange API."""
    import ccxt
    os.makedirs(os.path.dirname(path), exist_ok=True)
    last_err = None
    for name in ("binance", "bybit", "okx"):
        try:
            ex = getattr(ccxt, name)({"enableRateLimit": True})
            since = ex.parse8601("2017-08-17T00:00:00Z")
            rows = []
            while True:
                batch = ex.fetch_ohlcv("BTC/USDT", "1d", since=since, limit=1000)
                if not batch:
                    break
                rows += batch
                since = batch[-1][0] + 86_400_000
                print(f"  {name}: {len(rows)} days downloaded...", end="\r")
                if len(batch) < 2 or since > ex.milliseconds():
                    break
            # Drop today's unfinished candle.
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            out = []
            for t, o, h, l, c, _v in rows:
                d = datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%d")
                if d < today:
                    out.append((d, o, h, l, c))
            if len(out) < 400:
                raise RuntimeError(f"only {len(out)} days returned")
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["date", "open", "high", "low", "close"])
                w.writerows(out)
            print(f"\n  Saved {len(out)} days from {name} to {path}")
            return
        except Exception as e:  # region blocks, network errors
            last_err = e
            print(f"\n  {name} failed ({type(e).__name__}), trying next exchange")
    raise SystemExit(f"Could not download price history: {last_err}")


def load(path=DATA_FILE):
    b = Bars()
    with open(path) as f:
        for row in csv.DictReader(f):
            b.date.append(row["date"])
            for k in ("open", "high", "low", "close"):
                getattr(b, k).append(float(row[k]))
    return b


def synthetic(days=2500, seed=7):
    """Random prices with BTC-like volatility, for offline testing only."""
    rng = random.Random(seed)
    b = Bars()
    price = 4000.0
    start = datetime(2018, 1, 1, tzinfo=timezone.utc).timestamp()
    for i in range(days):
        o = price
        c = o * math.exp(rng.gauss(0.0007, 0.035))
        b.date.append(datetime.fromtimestamp(start + i * 86400, timezone.utc).strftime("%Y-%m-%d"))
        b.open.append(o)
        b.close.append(c)
        b.high.append(max(o, c) * (1 + abs(rng.gauss(0, 0.01))))
        b.low.append(min(o, c) * (1 - abs(rng.gauss(0, 0.01))))
        price = c
    return b

# ------------------------------------------------------------- indicators ---


def sma(xs, n):
    out, s = [None] * len(xs), 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rsi(xs, n=14):
    """Wilder's RSI."""
    out = [None] * len(xs)
    if len(xs) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = xs[i] - xs[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    out[n] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(xs)):
        d = xs[i] - xs[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    return out

# ------------------------------------------------------------- strategies ---
# Each strategy returns a list of target weights (0 = all cash, 1 = all BTC).
# weights[t] may only use data up to and including day t's close.


def buy_and_hold(b):
    return [1.0] * len(b)


def sma_crossover(b, fast=50, slow=200):
    f, s = sma(b.close, fast), sma(b.close, slow)
    return [1.0 if f[i] is not None and s[i] is not None and f[i] > s[i] else 0.0
            for i in range(len(b))]


def price_above_sma(b, n=200):
    m = sma(b.close, n)
    return [1.0 if m[i] is not None and b.close[i] > m[i] else 0.0 for i in range(len(b))]


def rsi_reversion(b, n=14, buy_below=30, sell_above=70):
    r = rsi(b.close, n)
    w, pos = [], 0.0
    for v in r:
        if v is not None:
            if pos == 0 and v < buy_below:
                pos = 1.0
            elif pos == 1 and v > sell_above:
                pos = 0.0
        w.append(pos)
    return w


def breakout(b, n=20):
    """Buy a new n-day high, sell a new n-day low (Donchian channel)."""
    w, pos = [], 0.0
    for i in range(len(b)):
        if i >= n:
            hi = max(b.high[i - n:i])
            lo = min(b.low[i - n:i])
            if b.close[i] > hi:
                pos = 1.0
            elif b.close[i] < lo:
                pos = 0.0
        w.append(pos)
    return w


def dca_12_months(b):
    """Spread the money evenly over 12 monthly buys, then hold."""
    return [min(1.0, (i // 30 + 1) / 12) for i in range(len(b))]


STRATEGIES = {
    "Buy and hold": buy_and_hold,
    "Monthly buys over 1 year": dca_12_months,
    "50/200-day crossover": sma_crossover,
    "Price above 200-day avg": price_above_sma,
    "RSI dip buying (30/70)": rsi_reversion,
    "20-day breakout": breakout,
}

# ----------------------------------------------------------------- engine ---


@dataclass
class Result:
    name: str
    equity: list
    trades: int
    fees_paid: float
    exposure: float          # share of days holding any BTC


def run(b, weights, capital=1000.0, fee=0.001, slippage=0.0005, name=""):
    """
    Day 0: start in cash. On day t (t >= 1) we look at weights[t-1], decided at
    yesterday's close, and trade to that weight at today's open, paying fee
    and slippage. Equity is marked at each day's close.
    """
    cash, coins = capital, 0.0
    equity = [capital]
    trades = 0
    fees_paid = 0.0
    held_days = 0
    last_target = 0.0
    for t in range(1, len(b)):
        target = weights[t - 1]
        px = b.open[t]
        value = cash + coins * px
        want_coins = target * value / px
        diff = want_coins - coins
        # Only trade when the signal changes; no daily rebalancing.
        if target != last_target and abs(diff) * px > 1e-9 * value:
            last_target = target
            if diff > 0:   # buy
                fill = px * (1 + slippage)
                spend = min(cash, diff * fill * (1 + fee))
                got = spend / (fill * (1 + fee))
                cash -= spend
                coins += got
                fees_paid += spend - got * px
            else:          # sell
                fill = px * (1 - slippage)
                qty = -diff
                proceeds = qty * fill * (1 - fee)
                coins -= qty
                cash += proceeds
                fees_paid += qty * px - proceeds
            trades += 1
        if coins > 0:
            held_days += 1
        equity.append(cash + coins * b.close[t])
    return Result(name, equity, trades, fees_paid, held_days / max(1, len(b) - 1))


def metrics(r, capital):
    eq = r.equity
    years = (len(eq) - 1) / DAYS_PER_YEAR
    total = eq[-1] / capital - 1
    cagr = (eq[-1] / capital) ** (1 / years) - 1 if years > 0 and eq[-1] > 0 else float("nan")
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
    mean = sum(rets) / len(rets) if rets else 0
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1) if len(rets) > 1 else 0
    sharpe = mean / math.sqrt(var) * math.sqrt(DAYS_PER_YEAR) if var > 0 else 0.0
    return {"final": eq[-1], "total": total, "cagr": cagr, "mdd": mdd, "sharpe": sharpe,
            "trades": r.trades, "fees": r.fees_paid, "exposure": r.exposure}

# ----------------------------------------------------------------- output ---


def table(results, capital, title, console):
    from rich.table import Table
    rows = [(r, metrics(r, capital)) for r in results]
    hold = next((m for r, m in rows if r.name == "Buy and hold"), None)
    t = Table(title=title, title_justify="left")
    for c, j in (("Strategy", "left"), ("Final $", "right"), ("Total", "right"),
                 ("Per year", "right"), ("Worst drop", "right"), ("Sharpe", "right"),
                 ("Trades", "right"), ("Fees $", "right"), ("In market", "right"),
                 ("vs hold", "left")):
        t.add_column(c, justify=j)
    for r, m in sorted(rows, key=lambda x: -x[1]["final"]):
        verdict = ""
        if hold and r.name != "Buy and hold":
            better_ret = m["final"] > hold["final"]
            better_risk = m["sharpe"] > hold["sharpe"]
            verdict = ("[green]beat it[/green]" if better_ret else
                       "[yellow]less return, smoother ride[/yellow]" if better_risk else
                       "[red]worse[/red]")
        t.add_row(r.name, f"{m['final']:,.0f}", f"{m['total']*100:+,.0f}%",
                  f"{m['cagr']*100:+.1f}%", f"{m['mdd']*100:.0f}%", f"{m['sharpe']:.2f}",
                  str(m["trades"]), f"{m['fees']:,.0f}", f"{m['exposure']*100:.0f}%", verdict)
    console.print(t)
    return rows


def chart(b, results, path, title):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        return None
    dates = [datetime.strptime(d, "%Y-%m-%d") for d in b.date]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1.3]})
    for r in results:
        lw = 2.4 if r.name == "Buy and hold" else 1.3
        ax1.plot(dates, r.equity, label=r.name, linewidth=lw)
        peak, dd = r.equity[0], []
        for v in r.equity:
            peak = max(peak, v)
            dd.append((v / peak - 1) * 100)
        ax2.plot(dates, dd, linewidth=lw if lw > 2 else 0.9)
    ax1.set_yscale("log")
    ax1.set_ylabel("Account value ($, log scale)")
    ax1.set_title(title, loc="left", fontsize=12)
    ax1.legend(fontsize=8, frameon=False, loc="upper left")
    ax1.grid(alpha=0.25)
    ax2.set_ylabel("Drop from peak (%)")
    ax2.grid(alpha=0.25)
    ax2.xaxis.set_major_locator(mdates.YearLocator())
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    for ax in (ax1, ax2):
        ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path

# ------------------------------------------------------------ overfitting ---


def optimize(b, args, console):
    """
    Grid-search the moving-average crossover on the first part of history
    (in-sample), then run the winner on the rest (out-of-sample).
    """
    from rich.table import Table
    split = int(len(b) * args.split)
    warm = 250                                   # days of history for the averages
    train = b.slice(0, split)
    test = b.slice(max(0, split - warm), len(b))  # includes warm-up history

    fasts = [5, 10, 20, 30, 50, 75, 100]
    slows = [50, 100, 150, 200, 250]
    grid = []
    for f in fasts:
        for s in slows:
            if f >= s:
                continue
            r = run(train, sma_crossover(train, f, s), args.capital, args.fee, args.slippage)
            grid.append((f, s, metrics(r, args.capital)["final"]))
    grid.sort(key=lambda x: -x[2])
    hold_train = metrics(run(train, buy_and_hold(train), args.capital, args.fee, args.slippage),
                         args.capital)["final"]

    console.print(f"\n[bold]Overfitting test[/bold]: tuning the moving-average crossover on "
                  f"{train.date[0]} to {train.date[-1]}, then testing on "
                  f"{b.date[split]} to {b.date[-1]}.\n"
                  f"Tried {len(grid)} combinations of fast/slow averages.\n")

    cut = split - max(0, split - warm)
    unseen = test.slice(cut, len(test))

    def oos(weights_fn):
        # indicators see the warm-up days; trading only happens on unseen days
        r = run(unseen, weights_fn(test)[cut:], args.capital, args.fee, args.slippage)
        return metrics(r, args.capital)["final"]

    hold_test = oos(buy_and_hold)
    t = Table(title="Best 5 settings from the tuning period", title_justify="left")
    for c in ("Fast", "Slow", "Tuning period $", "Hold, tuning $", "Unseen period $", "Hold, unseen $"):
        t.add_column(c, justify="right")
    for f, s, fin in grid[:5]:
        test_fin = oos(lambda x, f=f, s=s: sma_crossover(x, f, s))
        colour = "green" if test_fin > hold_test else "red"
        t.add_row(str(f), str(s), f"{fin:,.0f}", f"{hold_train:,.0f}",
                  f"[{colour}]{test_fin:,.0f}[/{colour}]", f"{hold_test:,.0f}")
    console.print(t)
    rank_in = {(f, s): i for i, (f, s, _) in enumerate(grid)}
    oos_all = sorted(((oos(lambda x, f=f, s=s: sma_crossover(x, f, s)), f, s)
                      for f, s, _ in grid), reverse=True)
    bf, bs = oos_all[0][1], oos_all[0][2]
    console.print(
        f"\nThe best settings on unseen data were {bf}/{bs}, which ranked "
        f"#{rank_in[(bf, bs)] + 1} of {len(grid)} during tuning.\n"
        "If the tuning winner isn't the unseen winner, the 'best' settings were "
        "fitted to past noise. That's overfitting, and it's why a strategy that "
        "looks great in a backtest often fails live.")

# ------------------------------------------------------------------- main ---


def main():
    p = argparse.ArgumentParser(description="BTC strategy backtester (paper only)")
    p.add_argument("--demo", action="store_true", help="synthetic prices, offline")
    p.add_argument("--refresh", action="store_true", help="re-download price history")
    p.add_argument("--start", help="first date to test, e.g. 2021-01-01")
    p.add_argument("--end", help="last date to test")
    p.add_argument("--capital", type=float, default=1000.0)
    p.add_argument("--fee", type=float, default=0.001, help="per trade, 0.001 = 0.1%%")
    p.add_argument("--slippage", type=float, default=0.0005, help="per trade, 0.0005 = 0.05%%")
    p.add_argument("--optimize", action="store_true", help="run the overfitting test")
    p.add_argument("--split", type=float, default=0.6, help="share of history used for tuning")
    p.add_argument("--no-chart", action="store_true")
    args = p.parse_args()

    from rich.console import Console
    console = Console()

    if args.demo:
        b = synthetic()
        console.print("[yellow]DEMO: synthetic prices. Results mean nothing about real BTC.[/yellow]")
    else:
        if args.refresh or not os.path.exists(DATA_FILE):
            console.print("Downloading daily BTC/USDT history (one time)...")
            download()
        b = load()

    # Keep 250 days before --start so long averages are ready on day one.
    lo = b.index_of(args.start) if args.start else 0
    hi = b.index_of(args.end) + 1 if args.end else len(b)
    hi = min(hi, len(b))
    warm = 250 if args.start else 0
    full = b.slice(max(0, lo - warm), hi)
    cut = lo - max(0, lo - warm)

    if args.optimize:
        optimize(b.slice(lo, hi), args, console)
        return

    # Indicators are computed on the warm-up history too, but trading starts at --start.
    shown = full.slice(cut, len(full))
    results = []
    for name, fn in STRATEGIES.items():
        w = fn(shown) if name == "Monthly buys over 1 year" else fn(full)[cut:]
        results.append(run(shown, w, args.capital, args.fee, args.slippage, name))

    title = (f"${args.capital:,.0f} in BTC, {shown.date[0]} to {shown.date[-1]}  "
             f"(fee {args.fee*100:.2f}% + slippage {args.slippage*100:.2f}% per trade)")
    table(results, args.capital, title, console)
    console.print(
        "[dim]Worst drop = largest fall from a previous high. Sharpe = return per unit "
        "of day-to-day risk (higher is smoother). In market = share of days holding BTC.[/dim]")

    os.makedirs("results", exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(f"results/summary_{stamp}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "final", "total_return", "cagr", "max_drawdown",
                    "sharpe", "trades", "fees", "exposure"])
        for r in results:
            m = metrics(r, args.capital)
            w.writerow([r.name, f"{m['final']:.2f}", f"{m['total']:.4f}", f"{m['cagr']:.4f}",
                        f"{m['mdd']:.4f}", f"{m['sharpe']:.3f}", m["trades"],
                        f"{m['fees']:.2f}", f"{m['exposure']:.3f}"])
    if not args.no_chart:
        path = chart(shown, results, f"results/chart_{stamp}.png", title)
        if path:
            console.print(f"Chart saved to [bold]{path}[/bold]  (open it with: open {path})")
        else:
            console.print("[dim]Install matplotlib for a chart: python3 -m pip install matplotlib[/dim]")
    console.print(f"Numbers saved to results/summary_{stamp}.csv")


if __name__ == "__main__":
    main()
