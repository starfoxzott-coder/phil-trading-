"""Where exactly to put the stop and the target for THIS signal, and whether it is worth it.

For one entry it scores every pair of stop rule x target R on two kinds of evidence:

  simulation   2,000 replays of the rest of today built from this stock's own
               bars (of the chosen length) of the history Yahoo keeps (the trader-timemachine method).
               Every pair is scored on the SAME paths, so the comparison is fair.
  history      this stock's own past breakouts under the same entry rule, each
               re-walked bar by bar with that stop and target.

Only targets that the replays reach first at least 15 % of the time count: a
target that is almost never hit is no take-profit, the trade would just be
held to the close. The two are blended (history weighs more the more past breakouts there are),
round-trip trading costs are taken off, and the pair with the highest expected
R wins, but only if it beats the strategy's default pair by a margin, so the
choice does not chase noise. Long side only; a bar that touches both levels
counts as the stop.
"""

import bisect
import random

import engine

STOPS = ["or-mid", "or-low", "atr:1", "atr:1.5", "atr:2", "vwap"]
TARGETS = [1.0, 1.5, 2.0, 2.5, 3.0]
STOP_LABEL = {"or-mid": "Mitte der Eröffnungsspanne", "or-low": "Tief der Eröffnungsspanne",
              "atr:1": "1 × ATR unter Einstieg", "atr:1.5": "1,5 × ATR unter Einstieg",
              "atr:2": "2 × ATR unter Einstieg", "vwap": "knapp unter VWAP"}
HIST_WEIGHT = 20      # past breakouts count fully once there are about this many
SWITCH_MARGIN = 0.03  # R a pair must beat the default by
MAX_RISK_PCT = 4.0    # a stop wider than this share of the price is not a day trade
MIN_TARGET_HIT = 0.15  # a target reached first in fewer replays than this is not a real take-profit


def stop_price(kind, entry, orr, atr_val, vwap):
    if kind == "or-mid":
        s = (orr["high"] + orr["low"]) / 2
    elif kind == "or-low":
        s = orr["low"]
    elif kind.startswith("atr:"):
        if not atr_val:
            return None
        s = entry - float(kind[4:]) * atr_val
    elif kind == "vwap":
        if vwap is None or not atr_val or vwap >= entry:
            return None
        s = vwap - 0.1 * atr_val
    else:
        return None
    s = engine.floor_cent(s)
    risk = entry - s
    if risk <= 0 or risk < 0.0005 * entry or risk > MAX_RISK_PCT / 100 * entry:
        return None
    return s


def plan_for(kind, r_mult, entry, orr, atr_val, vwap):
    s = stop_price(kind, entry, orr, atr_val, vwap)
    if s is None:
        return None
    risk = entry - s
    return {"entry": round(entry, 2), "stop": s, "target": engine.round_cent(entry + r_mult * risk),
            "risk": round(risk, 4), "target_r": r_mult, "stop_kind": kind}


# ---------------------------------------------------------------- simulation paths

def paths(sess, today, start_slot, end_slot, bar, replays=2000, seed=19):
    """Relative paths from the entry: running max high, running min low, close, per step.

    Each step draws a past bar from about the same time of day (within 15 minutes).
    """
    nb = engine.full_bars(bar)
    window = max(1, 900 // bar)
    full = [day for d, day in sess.items() if d != today and len(day) == nb]
    if len(full) < 10 or end_slot <= start_slot:
        return None
    pool = [[] for _ in range(nb)]
    for day in full:
        for j in range(1, nb):
            pc = day[j - 1]["c"]
            b = day[j]
            pool[j].append((b["h"] / pc, b["l"] / pc, b["c"] / pc))
    rng = random.Random(seed)
    out = []
    for _ in range(replays):
        px, mh, ml = 1.0, 0.0, 9e9
        his, neg_los, closes = [], [], []
        for slot in range(start_slot, end_slot):
            j = min(nb - 1, max(1, slot + rng.randint(-window, window)))
            h, l, c = rng.choice(pool[j])
            mh, ml = max(mh, px * h), min(ml, px * l)
            px *= c
            his.append(mh)
            neg_los.append(-ml)
            closes.append(px)
        out.append((his, neg_los, closes))
    return {"paths": out, "sessions": len(full), "start_slot": start_slot, "end_slot": end_slot}


def score_paths(P, plan):
    """Outcome odds and mean R of one plan on the shared paths (first passage by bisection)."""
    e = plan["entry"]
    sr, tr = plan["stop"] / e, plan["target"] / e
    n_t = n_s = 0
    total = 0.0
    exits = []
    for his, neg_los, closes in P["paths"]:
        i_s = bisect.bisect_left(neg_los, -sr)
        i_t = bisect.bisect_left(his, tr)
        if i_s < len(closes) and i_s <= i_t:
            n_s += 1
            total -= 1.0
            exits.append(i_s)
        elif i_t < len(closes):
            n_t += 1
            total += plan["target_r"]
            exits.append(i_t)
        else:
            total += (closes[-1] - 1.0) * e / plan["risk"]
    n = len(P["paths"])
    exits.sort()
    return {"p_target": n_t / n, "p_stop": n_s / n, "p_neither": (n - n_t - n_s) / n,
            "ev_r": total / n, "median_exit_step": exits[len(exits) // 2] if exits else None}


def fan(P, entry):
    steps = P["end_slot"] - P["start_slot"]
    out = []
    for s in range(steps):
        v = sorted(p[2][s] * entry for p in P["paths"])
        q = lambda x: v[min(len(v) - 1, int(x * len(v)))]  # noqa: E731
        out.append({"slot": P["start_slot"] + s, "p5": q(.05), "p25": q(.25), "p50": q(.5), "p75": q(.75), "p95": q(.95)})
    return out


# ---------------------------------------------------------------- past breakouts

def past_breakouts(sess, params, rules, skip_date):
    out = []
    for d, day in sess.items():
        if d == skip_date or len(day) != engine.full_bars(day[0]["d"]):
            continue
        orr = engine.opening_range(day, params["or_minutes"])
        if not orr:
            continue
        i = engine.find_trigger(day, orr, params, rules)
        if i is None:
            continue
        out.append((day, i, orr, engine.atr(day[:i + 1]), day[i]["vwap"]))
    return out


def score_history(breakouts, kind, r_mult, flat_min):
    rs = []
    for day, i, orr, atr_val, vwap in breakouts:
        p = plan_for(kind, r_mult, day[i]["c"], orr, atr_val, vwap)
        if not p:
            continue
        res = engine.walk(day, i + 1, p, flat_min)
        if res:
            rs.append(res["r"])
    return rs


# ---------------------------------------------------------------- the choice

def choose(sess, today, entry, orr, atr_val, vwap, start_slot, params, rules, acct, replays=2000):
    flat_min = engine.hhmm_to_min(rules["flat_by"])
    bar = next(iter(sess.values()))[0]["d"]
    end_slot = min(engine.full_bars(bar), (flat_min - engine.OPEN_MIN) * 60 // bar)
    P = paths(sess, today, start_slot, end_slot, bar, replays)
    if not P:
        return None
    hist = past_breakouts(sess, params, rules, today)
    cost_bps = float(rules.get("cost_bps", 3))
    rows, best, default = [], None, None
    for kind in STOPS:
        for r_mult in TARGETS:
            p = plan_for(kind, r_mult, entry, orr, atr_val, vwap)
            if not p:
                rows.append({"stop_kind": kind, "target_r": r_mult, "valid": False})
                continue
            sim = score_paths(P, p)
            hr = score_history(hist, kind, r_mult, flat_min)
            n_h = len(hr)
            ev_h = sum(hr) / n_h if n_h else 0.0
            ev = (n_h * ev_h + HIST_WEIGHT * sim["ev_r"]) / (n_h + HIST_WEIGHT)
            cost_r = entry * 2 * cost_bps / 1e4 / p["risk"]
            shares = engine.size(p, acct)["shares"]
            row = dict(p, valid=shares > 0, reachable=sim["p_target"] >= MIN_TARGET_HIT, ev_net=round(ev - cost_r, 3), ev_sim=round(sim["ev_r"], 3),
                       ev_hist=round(ev_h, 3) if n_h else None, n_hist=n_h, cost_r=round(cost_r, 3),
                       p_target=round(sim["p_target"], 3), p_stop=round(sim["p_stop"], 3),
                       p_neither=round(sim["p_neither"], 3), median_exit_step=sim["median_exit_step"],
                       hist_win=round(sum(1 for r in hr if r > 0) / n_h, 3) if n_h else None)
            rows.append(row)
            if not row["valid"] or not row["reachable"]:
                continue
            if best is None or row["ev_net"] > best["ev_net"]:
                best = row
            if kind == params["stop"] and r_mult == params["target_r"]:
                default = row
    if best is None:
        return None
    chosen = best
    if default and best is not default and best["ev_net"] - default["ev_net"] < SWITCH_MARGIN:
        chosen = default
    return {"chosen": chosen, "default": default, "rows": rows, "paths": P, "n_past_breakouts": len(hist)}


def describe_choice(c, why_default=False):
    k = STOP_LABEL.get(c["stop_kind"], c["stop_kind"])
    return "Stop %s (%s), Ziel %gR" % (c["stop"], k, c["target_r"]) + (" – Standard der Regel" if why_default else "")

