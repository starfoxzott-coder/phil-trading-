"""The learning loop: Phil's settle -> score -> retro -> edit-strategy cycle, for stocks.

  1. settle   every recorded signal whose day is over gets its real outcome
              (stop / target / flat at flat_by) from the 5-minute bars
  2. score    win rate, average R, and calibration: did the Time Machine's
              "target first" odds match what really happened?
  3. evolve   re-test every rule variant on the last 60 days of the watchlist
              plus the settled live signals; switch only when the new variant's
              lower confidence bound (mean R minus one standard error) beats the
              current one by a margin, so one lucky week does not flip the rule
  4. retro    append what changed and why to data/retros.md, every edit to
              data/strategy-history.jsonl (the app's own "git log" of lessons)

Paper only: a signal is tracked whether or not you took it. Marking a signal
as taken only makes it count toward your daily trade and loss limits.
"""

import datetime
import itertools
import json
import os
import time

import engine

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
STRATEGY = os.path.join(DATA, "strategy.json")
JOURNAL = os.path.join(DATA, "journal.jsonl")
HISTORY = os.path.join(DATA, "strategy-history.jsonl")
RETROS = os.path.join(DATA, "retros.md")

GRID = {
    "or_minutes": [15, 30],
    "stop": ["or-mid", "or-low"],
    "target_r": [1.0, 1.5, 2.0, 3.0],
    "vol_x": [0, 1.5],
    "trend": ["none", "vwap", "ema"],
}
MIN_SAMPLES = 30          # a variant needs this many trades to be chosen
SWITCH_MARGIN = 0.03      # R the lower bound must improve by before switching
BLOCK_AFTER = 10          # trades on one symbol before it can be blocked
BLOCK_BELOW = -0.15       # average R that blocks a symbol
CALIB_MIN = 10            # settled signals before the edge filter is tuned


def load_strategy():
    with open(STRATEGY) as f:
        return json.load(f)


def save_strategy(s):
    tmp = STRATEGY + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, STRATEGY)


def read_journal():
    if not os.path.exists(JOURNAL):
        return []
    with open(JOURNAL) as f:
        return [json.loads(line) for line in f if line.strip()]


def write_journal(rows):
    tmp = JOURNAL + ".tmp"
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    os.replace(tmp, JOURNAL)


def _append(path, text):
    os.makedirs(DATA, exist_ok=True)
    with open(path, "a") as f:
        f.write(text)


def today_trades(date_iso):
    return [r for r in read_journal() if r["date"] == date_iso and r.get("taken")]


def record_signal(a):
    """Store a signal the app judged good once per symbol and day, with the odds it gave.

    Recorded whether it is still fresh (KAUFEN), running (LÄUFT) or already over
    (VORBEI), so learning does not depend on when you happened to open the app.
    """
    if not a.get("signal_ok") or not a.get("plan"):
        return None
    rows = read_journal()
    for r in rows:
        if r["symbol"] == a["symbol"] and r["date"] == a["date"]:
            return r
    p = a["plan"]
    row = {"id": "%s-%s" % (a["symbol"], a["date"]), "symbol": a["symbol"], "date": a["date"],
           "entry_t": p["entry_t"], "entry": p["entry"], "stop": p["stop"], "target": p["target"],
           "risk": p["risk"], "target_r": p["target_r"], "params": a["params"],
           "p_target_share": (a.get("sim") or {}).get("target_share"),
           "edge_pp": (a.get("sim") or {}).get("edge_pp"),
           "shares": a["size"]["shares"], "loss_at_stop": a["size"]["loss_at_stop"],
           "liquid_read": (a.get("intel") or {}).get("read"),
           "stop_kind": p.get("stop_kind"), "ev_r": (a.get("sim") or {}).get("ev_net"),
           "grade": (a.get("quality") or {}).get("grade"),
           "checks": {c["key"]: c["ok"] for c in a.get("checks", [])},
           "smart_long": (a.get("intel") or {}).get("smart_long"),
           "funding_pct_hr": (a.get("perp") or {}).get("funding_pct_hr"),
           "taken": False, "status": "open", "recorded_at": int(time.time())}
    rows.append(row)
    write_journal(rows)
    return row


def mark_taken(signal_id, taken=True):
    rows = read_journal()
    for r in rows:
        if r["id"] == signal_id:
            r["taken"] = bool(taken)
    write_journal(rows)


# ---------------------------------------------------------------- 1. settle

def settle(strategy, now_ts=None):
    now_ts = now_ts or time.time()
    rows = read_journal()
    flat_min = engine.hhmm_to_min(strategy["rules"]["flat_by"])
    settled = []
    for r in rows:
        if r["status"] != "open":
            continue
        try:
            sess = engine.sessions(engine.fetch_bars(r["symbol"], "60d", "5m")["bars"], now_ts)
        except engine.EngineError:
            continue
        day = sess.get(r["date"])
        if not day:
            continue
        start = next((k for k, b in enumerate(day) if b["t"] >= r["entry_t"]), None)
        if start is None:
            continue
        res = engine.walk(day, start, r, flat_min)
        if res:
            r.update(status="settled", outcome=res["outcome"], r=res["r"], exit=res["exit"],
                     exit_t=res["exit_t"], bars_held=res["bars"])
            settled.append(r)
    write_journal(rows)
    return settled


# ---------------------------------------------------------------- 2. score

def score(rows=None):
    rows = rows if rows is not None else read_journal()
    done = [r for r in rows if r["status"] == "settled"]
    s = engine.summarize(done)
    decided = [r for r in done if r["outcome"] in ("target", "stop") and r.get("p_target_share") is not None]
    if decided:
        pred = sum(r["p_target_share"] for r in decided) / len(decided)
        real = sum(1 for r in decided if r["outcome"] == "target") / len(decided)
        brier = sum((r["p_target_share"] - (r["outcome"] == "target")) ** 2 for r in decided) / len(decided)
        s["calibration"] = {"n": len(decided), "predicted": round(pred, 3), "actual": round(real, 3),
                            "brier": round(brier, 4)}
    fast = [r for r in done if r["outcome"] == "stop" and r.get("bars_held", 99) <= 2]
    stops = [r for r in done if r["outcome"] == "stop"]
    s["false_breakouts"] = len(fast)
    s["false_breakout_share"] = round(len(fast) / len(stops), 3) if stops else None
    s["taken"] = engine.summarize([r for r in done if r.get("taken")])
    # does Liquid's positioning separate good signals from bad ones? (evidence, not yet a rule)
    s["by_liquid"] = {k: engine.summarize([r for r in done if r.get("liquid_read") == k])
                      for k in ("with", "neutral", "against")}
    s["by_grade"] = {g: engine.summarize([r for r in done if r.get("grade") == g]) for g in "ABC"}
    # each extra criterion: average R when it held vs when it did not
    s["by_check"] = {}
    for key in ("hist", "vwap", "market", "time", "rsi", "liquid"):
        yes = [r for r in done if (r.get("checks") or {}).get(key) is True]
        no = [r for r in done if (r.get("checks") or {}).get(key) is False]
        if yes or no:
            s["by_check"][key] = {"yes": engine.summarize(yes), "no": engine.summarize(no)}
    ev = [r for r in done if r.get("ev_r") is not None]
    if ev:
        s["ev_calibration"] = {"n": len(ev), "predicted": round(sum(r["ev_r"] for r in ev) / len(ev), 3),
                               "actual": round(sum(r["r"] for r in ev) / len(ev), 3)}
    return s


# ---------------------------------------------------------------- 3. evolve

def _variants():
    keys = list(GRID)
    for combo in itertools.product(*(GRID[k] for k in keys)):
        yield dict(zip(keys, combo))


def _same(a, b):
    return all(a.get(k, "none" if k == "trend" else None) == b.get(k, "none" if k == "trend" else None) for k in GRID)


def evolve(strategy, watchlist, now_ts=None):
    now_ts = now_ts or time.time()
    rules = strategy["rules"]
    sessions = {}
    for sym in watchlist:
        try:
            sessions[sym] = engine.sessions(engine.fetch_bars(sym, "60d", "5m")["bars"], now_ts)
        except engine.EngineError:
            pass
    live = [r for r in read_journal() if r["status"] == "settled"]

    table = []
    for v in _variants():
        trades, per_sym = [], {}
        for sym, sess in sessions.items():
            t = engine.backtest(sess, v, rules)
            per_sym[sym] = t
            trades += t
        mine = [r for r in live if _same(r["params"], v)]  # live signals count as fresh evidence
        trades += mine
        for r in mine:
            per_sym.setdefault(r["symbol"], []).append(r)
        table.append((v, engine.summarize(trades), per_sym))

    cur = next((t for t in table if _same(t[0], strategy["params"])), None)
    if cur is None:  # a hand-edited rule outside the grid: score it the same way
        per_sym = {sym: engine.backtest(sess, strategy["params"], rules) for sym, sess in sessions.items()}
        cur = (dict(strategy["params"]), engine.summarize(sum(per_sym.values(), [])), per_sym)
    ok = [t for t in table if t[1]["n"] >= MIN_SAMPLES]
    best = max(ok, key=lambda t: t[1]["lcb"]) if ok else cur
    changes = []

    if not _same(best[0], cur[0]) and best[1]["lcb"] - (cur[1]["lcb"] or -9) > SWITCH_MARGIN:
        changes.append({"field": "params", "old": cur[0], "new": best[0],
                        "why": "Variante %s: Ø %+.2fR über %d Trades (untere Schranke %+.2f) schlägt die aktuelle (Ø %+.2fR, n=%d, Schranke %+.2f)" % (
                            describe(best[0]), best[1]["avg_r"], best[1]["n"], best[1]["lcb"],
                            cur[1]["avg_r"] or 0, cur[1]["n"], cur[1]["lcb"] or 0)})
        strategy["params"] = dict(best[0])
        chosen = best
    else:
        chosen = cur

    # per-symbol blocks under the rule actually in use
    blocked = dict(strategy.get("blocked_symbols", {}))
    for sym, t in chosen[2].items():
        s = engine.summarize(t)
        if s["n"] >= BLOCK_AFTER and s["avg_r"] < BLOCK_BELOW:
            why = "Ø %+.2fR über %d Ausbrüche" % (s["avg_r"], s["n"])
            if blocked.get(sym) != why:
                changes.append({"field": "blocked_symbols." + sym, "old": blocked.get(sym), "new": why,
                                "why": "Regel verliert auf %s systematisch" % sym})
            blocked[sym] = why
        elif sym in blocked and s["n"] >= BLOCK_AFTER and s["avg_r"] > 0:
            changes.append({"field": "blocked_symbols." + sym, "old": blocked[sym], "new": None,
                            "why": "%s wieder positiv (Ø %+.2fR, n=%d)" % (sym, s["avg_r"], s["n"])})
            del blocked[sym]
    strategy["blocked_symbols"] = blocked

    # calibration: if the predicted expectation was too optimistic, demand more of it
    sc = score(live)
    cal = sc.get("ev_calibration")
    if cal and cal["n"] >= CALIB_MIN:
        gap = cal["predicted"] - cal["actual"]
        old = strategy.get("min_ev_r", 0.10)
        new = old
        if gap > 0.15:
            new = min(0.5, old + 0.05)
        elif gap < -0.15:
            new = max(0.0, old - 0.05)
        new = round(new, 2)
        if new != old:
            strategy["min_ev_r"] = new
            changes.append({"field": "min_ev_r", "old": old, "new": new,
                            "why": "Vorhergesagt Ø %+.2fR, real Ø %+.2fR über %d Signale" % (
                                cal["predicted"], cal["actual"], cal["n"])})

    ranking = sorted(({"params": t[0], "label": describe(t[0]), **t[1]} for t in ok),
                     key=lambda x: -x["lcb"])[:8]
    return changes, ranking, {"chosen": describe(chosen[0]), **chosen[1]}, sc


def describe(p):
    vol = (" · Vol ≥%.1fx" % p["vol_x"]) if p["vol_x"] else ""
    trend = {"vwap": " · über VWAP", "ema": " · über EMA20"}.get(p.get("trend", "none"), "")
    return "ORB %d Min · Stop %s · Ziel %gR%s%s" % (p["or_minutes"], p["stop"], p["target_r"], vol, trend)


# ---------------------------------------------------------------- 4. the cycle

def cycle(now_ts=None):
    strategy = load_strategy()
    watchlist = strategy["watchlist"]
    settled = settle(strategy, now_ts)
    changes, ranking, current, sc = evolve(strategy, watchlist, now_ts)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    strategy["learned_at"] = stamp
    strategy["generation"] = strategy.get("generation", 0) + (1 if changes else 0)
    save_strategy(strategy)

    for c in changes:
        _append(HISTORY, json.dumps(dict(c, at=stamp, generation=strategy["generation"])) + "\n")
    lines = ["## Lernzyklus %s" % stamp, ""]
    lines.append("- Abgerechnet: %d Signal(e)" % len(settled))
    for r in settled:
        lines.append("  - %s %s: %s, %+.2fR" % (r["symbol"], r["date"], r["outcome"], r["r"]))
    if sc["n"]:
        lines.append("- Live-Bilanz: %d Signale, Trefferquote %.0f%%, Ø %+.2fR" % (sc["n"], sc["win_rate"] * 100, sc["avg_r"]))
    if sc.get("false_breakout_share") is not None:
        lines.append("- Fehlausbrüche (Stop in ≤2 Kerzen): %.0f%% der Stops" % (sc["false_breakout_share"] * 100))
    lines.append("- Aktive Regel: %s — Backtest Ø %+.2fR, n=%d" % (current["chosen"], current["avg_r"] or 0, current["n"]))
    if changes:
        lines.append("- Änderungen:")
        lines += ["  - **%s**: %s" % (c["field"], c["why"]) for c in changes]
    else:
        lines.append("- Keine Änderung: kein Kandidat schlägt die aktuelle Regel deutlich genug")
    _append(RETROS, "\n".join(lines) + "\n\n")
    return {"settled": settled, "changes": changes, "ranking": ranking, "current": current,
            "score": sc, "strategy": strategy}


def history():
    if not os.path.exists(HISTORY):
        return []
    with open(HISTORY) as f:
        return [json.loads(l) for l in f if l.strip()]
