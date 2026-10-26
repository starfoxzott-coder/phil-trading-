"""Data for the interactive chart window: long history in any bar length, and past signals.

The chart's bar length is only how you look at the price. The signal bar length
(rules.bar_minutes) is what the app trades on; past signals are always computed
on that one and drawn onto whatever bars you look at, by their timestamps.
"""

import engine
import learn

CHART_INTERVALS = ("1", "2", "5", "15", "30", "60", "D")


def _row(b):
    return [b["t"], round(b["o"], 4), round(b["h"], 4), round(b["l"], 4), round(b["c"], 4), b["v"],
            round(b["vwap"], 4) if b.get("vwap") is not None else None,
            round(b["ema"], 4) if b.get("ema") is not None else None]


def chart(symbol, iv):
    iv = str(iv)
    if iv not in CHART_INTERVALS:
        raise engine.EngineError("Chart-Kerzen: 1, 2, 5, 15, 30, 60 Minuten oder D (Tag)")
    if iv == "D":
        data = engine.fetch_bars(symbol, "2y", "1d", ttl=600)
        bars, ema, k = [], None, 2 / 21.0
        for b in data["bars"]:
            ema = b["c"] if ema is None else ema + k * (b["c"] - ema)
            bars.append(dict(b, ema=ema, vwap=None))
        return {"symbol": data["symbol"], "iv": iv, "bar": 86400, "intraday": False, "bars": [_row(b) for b in bars]}
    if iv == "60":
        data = engine.fetch_bars(symbol, "730d", "60m", ttl=300)
    else:
        data = engine.history(symbol, int(iv))
    sess = engine.sessions(data["bars"])
    bars = [b for day in sess.values() for b in day]
    return {"symbol": data["symbol"], "iv": iv, "bar": bars[0]["d"] if bars else int(iv) * 60, "intraday": True,
            "days": len(sess), "bars": [_row(b) for b in bars]}


def past_signals(symbol, minutes=None):
    """Every past day's breakout under the rule learned for the signal bar length, with its outcome.

    Uses the rule's own stop and target (the per-trade optimizer runs only for today).
    """
    strategy = learn.load_strategy(minutes)
    m = strategy["active_minutes"]
    sess = engine.sessions(engine.history(symbol, m)["bars"])
    today = engine.et_now().date().isoformat()
    rows = engine.backtest(sess, strategy["params"], strategy["rules"], skip_date=today)
    out = [{k: t[k] for k in ("date", "entry_t", "exit_t", "entry", "stop", "target", "target_r", "outcome", "r", "bars")}
           for t in rows]
    return {"symbol": symbol.upper(), "minutes": m, "rule": learn.describe(strategy["params"]),
            "signals": out, "summary": engine.summarize(rows)}
