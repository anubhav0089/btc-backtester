"""Offline tests: no network needed."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backtest as bt  # noqa: E402


def bars(closes, opens=None):
    opens = opens or closes
    return bt.Bars([f"2020-01-{i+1:02d}" for i in range(len(closes))],
                   list(opens), [c * 1.01 for c in closes], [c * 0.99 for c in closes],
                   list(closes))


def test_sma():
    assert bt.sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_rsi_extremes():
    up = list(range(1, 40))
    assert bt.rsi(up, 14)[-1] == 100
    down = list(range(40, 1, -1))
    assert bt.rsi(down, 14)[-1] < 1


def test_buy_and_hold_with_no_costs_matches_price_change():
    b = bars([100, 100, 150, 200], opens=[100, 100, 150, 200])
    r = bt.run(b, bt.buy_and_hold(b), capital=1000, fee=0, slippage=0)
    assert abs(r.equity[-1] - 2000) < 1e-6
    assert r.trades == 1


def test_signal_executes_next_day_not_same_day():
    """A signal decided on day t's close must trade at day t+1's open,
    so it can't profit from day t's own move (no look-ahead)."""
    closes = [100, 200, 200]
    opens = [100, 100, 200]   # jump happens during day 1
    b = bars(closes, opens)
    cheat = [0.0, 1.0, 1.0]   # "knows" on day 1 that day 1 went up
    r = bt.run(b, cheat, capital=1000, fee=0, slippage=0)
    # buys at day 2's open (200), so it never captures the 100 -> 200 jump
    assert abs(r.equity[-1] - 1000) < 1e-6


def test_costs_are_charged_on_round_trip():
    b = bars([100] * 4)
    r = bt.run(b, [1.0, 0.0, 0.0, 0.0], capital=1000, fee=0.001, slippage=0.0005)
    assert r.trades == 2
    assert 2.9 < 1000 - r.equity[-1] < 3.1     # ~0.15% each way
    assert abs(r.fees_paid - (1000 - r.equity[-1])) < 0.01


def test_only_trades_when_signal_changes():
    b = bars([100, 120, 90, 130, 80, 140])
    r = bt.run(b, [0.5] * 6, capital=1000, fee=0.001, slippage=0)
    assert r.trades == 1


def test_metrics_drawdown_and_total():
    r = bt.Result("x", [100, 150, 75, 120], 0, 0.0, 1.0)
    m = bt.metrics(r, 100)
    assert abs(m["mdd"] - (-0.5)) < 1e-9
    assert abs(m["total"] - 0.2) < 1e-9


def test_all_strategies_return_valid_weights():
    b = bt.synthetic(days=600)
    for name, fn in bt.STRATEGIES.items():
        w = fn(b)
        assert len(w) == len(b), name
        assert all(0.0 <= x <= 1.0 for x in w), name
