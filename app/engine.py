"""Phil Day-Trade engine: entry, stop, target, odds, size and risk for one stock.

Standard library only. Long side only. Nothing here places an order.

The rules follow the trader-* skills so the app and the skills agree:
  trader-entry       opening-range breakout: first COMPLETED 5-minute bar that
                     CLOSES above the range high (a wick is not a trigger)
  trader-exit        stop at the opening-range midpoint (rounded down to the
                     cent), target at R x risk, flat by 15:55 New York time
  trader-timemachine replays the rest of the session from the stock's own
                     5-minute bars of the last 60 days; stop checked before
                     the target in every bar (the conservative call)
  trader-budget      whole shares inside max loss, position cap, cash buffer
  trader-risk        stop required, loss at stop fits, R:R minimum, trades
                     per day, daily loss limit, entry window, market open

Which variant of the rule is used (opening-range length, stop rule, target R,
volume filter) is not fixed: it lives in data/strategy.json and learn.py
rewrites it from settled trades, the way Phil rewrites its playbook.

Data: Yahoo's free chart JSON (unofficial, can lag, bars not single trades).
"""

import datetime
import json
import math
import random
import threading
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (phil-daytrade-app)"
HOSTS = ("query2", "query1")
BAR = 300  # seconds in a 5-minute bar
OPEN_MIN = 9 * 60 + 30
CLOSE_MIN = 16 * 60
FULL_BARS = 78
FRESH_SECONDS = 600  # a trigger is a buy only while its bar closed in the last 10 minutes

_cache = {}
_cache_lock = threading.Lock()


class EngineError(Exception):
    pass


# ---------------------------------------------------------------- time (ET)

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def et_offset_hours(utc):
    """US Eastern offset: DST from the 2nd Sunday of March 2:00 to the 1st Sunday of November 2:00."""
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6))
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_et(ts):
    utc = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    return (utc + datetime.timedelta(hours=et_offset_hours(utc))).replace(tzinfo=None)


def et_now(now_ts=None):
    return to_et(now_ts if now_ts is not None else time.time())


def minute_of_day(dt):
    return dt.hour * 60 + dt.minute


def hhmm_to_min(text):
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def min_to_hhmm(m):
    return "%02d:%02d" % (m // 60, m % 60)


# ---------------------------------------------------------------- data

def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def yahoo(path):
    last = None
    for host in HOSTS:
        try:
            return _get("https://%s.finance.yahoo.com%s" % (host, path))
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code == 429:
                time.sleep(2)
            elif e.code < 500:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = str(e)
    raise EngineError("Yahoo antwortet nicht (%s)" % last)


def _cached(key, ttl, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (now, val)
    return val


def fetch_bars(symbol, rng="60d", interval="5m", ttl=60):
    """Bars as dicts {t, o, h, l, c, v} (t = bar start, epoch seconds) plus meta."""
    sym = symbol.upper().replace(".", "-")

    def load():
        doc = yahoo("/v8/finance/chart/%s?range=%s&interval=%s&includePrePost=false" % (sym, rng, interval))
        res = (doc.get("chart") or {}).get("result")
        if not res:
            raise EngineError("Keine Daten für %s" % sym)
        res = res[0]
        q = (res.get("indicators") or {}).get("quote", [{}])[0]
        bars = []
        step = 60 if interval == "1m" else BAR
        for i, t in enumerate(res.get("timestamp") or []):
            o, h, l, c = (q.get(k, [None])[i] for k in ("open", "high", "low", "close"))
            v = (q.get("volume") or [0])[i] or 0
            if None in (o, h, l, c) or t % step:
                continue  # drop Yahoo's live tick and empty bars
            bars.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": v})
        return {"symbol": sym, "meta": res.get("meta", {}), "bars": bars}

    return _cached(("bars", sym, rng, interval), ttl, load)


def fetch_news(symbol, count=6):
    sym = symbol.upper()

    def load():
        try:
            doc = yahoo("/v1/finance/search?q=%s&newsCount=%d&quotesCount=0" % (sym, count))
        except EngineError:
            return []
        out = []
        for n in doc.get("news", []):
            out.append({"title": n.get("title"), "publisher": n.get("publisher"),
                        "link": n.get("link"), "time": n.get("providerPublishTime"),
                        "tickers": n.get("relatedTickers") or []})
        return out

    return _cached(("news", sym), 600, load)


def sessions(bars, now_ts=None):
    """Regular-session bars grouped by New York date, only bars already closed."""
    now_ts = now_ts if now_ts is not None else time.time()
    out = {}
    for b in bars:
        if b["t"] + BAR > now_ts:
            continue
        et = to_et(b["t"])
        m = minute_of_day(et)
        if OPEN_MIN <= m < CLOSE_MIN:
            out.setdefault(et.date().isoformat(), []).append(b)
    out = dict(sorted(out.items()))
    annotate(out)
    return out


def annotate(sess):
    """Add session VWAP, a 20-bar EMA and a 14-bar RSI to every bar (once per bar dict).

    The EMA and RSI run across days on regular-session bars only; the VWAP
    restarts each morning. Each value uses only that bar and the bars before it.
    """
    k = 2 / 21.0
    ema = gain = loss = prev = None
    n = 0
    for day in sess.values():
        pv = vol = 0.0
        for b in day:
            if "vwap" not in b:
                typ = (b["h"] + b["l"] + b["c"]) / 3
                pv += typ * b["v"]
                vol += b["v"]
                b["vwap"] = pv / vol if vol else typ
            else:
                pv, vol = b["_pv"], b["_vol"]
            b["_pv"], b["_vol"] = pv, vol
            if "ema" not in b:
                ema = b["c"] if ema is None else ema + k * (b["c"] - ema)
                if prev is not None:
                    ch = b["c"] - prev
                    n += 1
                    g, l_ = max(ch, 0), max(-ch, 0)
                    if gain is None:
                        gain, loss = g, l_
                    else:  # Wilder smoothing
                        gain, loss = (gain * 13 + g) / 14, (loss * 13 + l_) / 14
                b["ema"] = ema
                b["rsi"] = 100 - 100 / (1 + gain / loss) if n >= 14 and loss else (100.0 if n >= 14 else None)
                b["_g"], b["_l"], b["_n"] = gain, loss, n
            else:
                ema, gain, loss, n = b["ema"], b["_g"], b["_l"], b["_n"]
            prev = b["c"]


def atr(day_bars, n=14):
    trs = []
    for i, b in enumerate(day_bars):
        pc = day_bars[i - 1]["c"] if i else b["o"]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    tail = trs[-n:]
    return sum(tail) / len(tail) if tail else None


# ---------------------------------------------------------------- the rule

def floor_cent(x):
    return math.floor(x * 100 + 1e-9) / 100


def round_cent(x):
    return math.floor(x * 100 + 0.5) / 100


def opening_range(day_bars, or_minutes):
    n = or_minutes // 5
    if len(day_bars) < n or minute_of_day(to_et(day_bars[0]["t"])) != OPEN_MIN:
        return None
    first = day_bars[:n]
    return {"high": max(b["h"] for b in first), "low": min(b["l"] for b in first),
            "avg_vol": sum(b["v"] for b in first) / n, "bars": n,
            "end": day_bars[n - 1]["t"] + BAR}


def make_plan(entry, orr, params):
    """trader-exit: stop and target for a long entry. Returns None when refused."""
    if params["stop"] == "or-low":
        stop = floor_cent(orr["low"])
    else:
        stop = floor_cent((orr["high"] + orr["low"]) / 2)
    risk = entry - stop
    if risk <= 0 or risk < 0.0005 * entry:
        return None
    target = round_cent(entry + params["target_r"] * risk)
    return {"entry": round(entry, 2), "stop": stop, "target": target,
            "risk": round(risk, 4), "target_r": params["target_r"]}


def find_trigger(day_bars, orr, params, rules):
    """trader-entry: first completed bar after the range that closes above the range high."""
    lo = hhmm_to_min(rules["no_entries_before"])
    hi = hhmm_to_min(rules["no_entries_after"])
    for i in range(orr["bars"], len(day_bars)):
        b = day_bars[i]
        close_min = minute_of_day(to_et(b["t"] + BAR))
        if close_min > hi:
            return None
        if b["c"] > orr["high"] and close_min >= lo:
            if params["vol_x"] and b["v"] < params["vol_x"] * orr["avg_vol"]:
                continue
            trend = params.get("trend", "none")
            if trend == "vwap" and b["c"] <= b["vwap"]:
                continue
            if trend == "ema" and b["c"] <= b["ema"]:
                continue
            return i
    return None


def walk(day_bars, start, plan, flat_min):
    """Follow a trade bar by bar: stop first, then target, else flat at flat_by."""
    for j in range(start, len(day_bars)):
        b = day_bars[j]
        end_min = minute_of_day(to_et(b["t"] + BAR))
        if b["l"] <= plan["stop"]:
            return {"outcome": "stop", "r": -1.0, "exit": plan["stop"], "exit_t": b["t"] + BAR, "bars": j - start + 1}
        if b["h"] >= plan["target"]:
            return {"outcome": "target", "r": plan["target_r"], "exit": plan["target"], "exit_t": b["t"] + BAR, "bars": j - start + 1}
        if end_min >= flat_min:
            r = (b["c"] - plan["entry"]) / plan["risk"]
            return {"outcome": "flat", "r": round(r, 3), "exit": b["c"], "exit_t": b["t"] + BAR, "bars": j - start + 1}
    return None  # still open


def backtest_day(day_bars, params, rules):
    orr = opening_range(day_bars, params["or_minutes"])
    if not orr:
        return None
    i = find_trigger(day_bars, orr, params, rules)
    if i is None:
        return None
    b = day_bars[i]
    plan = make_plan(b["c"], orr, params)
    if not plan:
        return None
    res = walk(day_bars, i + 1, plan, hhmm_to_min(rules["flat_by"]))
    if not res:
        return None
    return dict(res, entry_t=b["t"] + BAR, **plan)


def backtest(sess, params, rules, skip_date=None):
    trades = []
    for d, day in sess.items():
        if d == skip_date or len(day) != FULL_BARS:
            continue
        t = backtest_day(day, params, rules)
        if t:
            t["date"] = d
            trades.append(t)
    return trades


def summarize(trades):
    n = len(trades)
    if not n:
        return {"n": 0, "win_rate": None, "avg_r": None, "se": None, "lcb": None}
    rs = [t["r"] for t in trades]
    mean = sum(rs) / n
    var = sum((r - mean) ** 2 for r in rs) / (n - 1) if n > 1 else 1.0
    se = math.sqrt(var / n)
    wins = sum(1 for r in rs if r > 0)
    return {"n": n, "win_rate": round(wins / n, 3), "avg_r": round(mean, 3),
            "se": round(se, 3), "lcb": round(mean - se, 3),
            "targets": sum(1 for t in trades if t["outcome"] == "target"),
            "stops": sum(1 for t in trades if t["outcome"] == "stop"),
            "flats": sum(1 for t in trades if t["outcome"] == "flat")}


# ---------------------------------------------------------------- time machine

def time_machine(sess, today, plan, start_slot, flat_min, replays=2000, seed=19, window=3):
    """trader-timemachine: replay the rest of today from past bars (ratio bootstrap)."""
    full = [day for d, day in sess.items() if d != today and len(day) == FULL_BARS]
    if len(full) < 10:
        return None
    pool = [[] for _ in range(FULL_BARS)]
    for day in full:
        for j in range(1, FULL_BARS):
            pc = day[j - 1]["c"]
            b = day[j]
            pool[j].append((b["o"] / pc, b["h"] / pc, b["l"] / pc, b["c"] / pc))
    end_slot = min(FULL_BARS, (flat_min - OPEN_MIN) // 5)
    rng = random.Random(seed)
    hits = {"target": 0, "stop": 0, "neither": 0}
    rs, exit_slots = [], []
    steps = max(0, end_slot - start_slot)
    fan = [[] for _ in range(steps)]
    for k in range(replays):
        price = plan["entry"]
        done = None
        for s in range(steps):
            slot = start_slot + s
            j = min(FULL_BARS - 1, max(1, slot + rng.randint(-window, window)))
            o, h, l, c = rng.choice(pool[j])
            hi, lo, cl = price * h, price * l, price * c
            if done is None:
                if lo <= plan["stop"]:
                    done = ("stop", -1.0, slot)
                elif hi >= plan["target"]:
                    done = ("target", plan["target_r"], slot)
            price = cl
            fan[s].append(price)
        if done is None:
            done = ("neither", (price - plan["entry"]) / plan["risk"], end_slot - 1)
        hits[done[0]] += 1
        rs.append(done[1])
        if done[0] != "neither":
            exit_slots.append(done[2])

    def pct(a, q):
        a = sorted(a)
        return a[min(len(a) - 1, int(q * len(a)))]

    decided = hits["target"] + hits["stop"]
    share = hits["target"] / decided if decided else 0.0
    be = 1.0 / (1.0 + plan["target_r"])
    fan_out = [{"slot": start_slot + s, "p5": pct(v, .05), "p25": pct(v, .25), "p50": pct(v, .5),
                "p75": pct(v, .75), "p95": pct(v, .95)} for s, v in enumerate(fan) if v]
    return {"replays": replays, "sessions": len(full),
            "p_target": round(hits["target"] / replays, 3), "p_stop": round(hits["stop"] / replays, 3),
            "p_neither": round(hits["neither"] / replays, 3), "avg_r": round(sum(rs) / replays, 3),
            "target_share": round(share, 3), "break_even": round(be, 3),
            "edge_pp": round((share - be) * 100, 1),
            "median_exit_slot": int(pct(exit_slots, .5)) if exit_slots else end_slot - 1,
            "fan": fan_out}


def verdict(edge_pp):
    a = abs(edge_pp)
    if a < 5:
        return "Nahe Break-even"
    side = "Richtung Ziel" if edge_pp > 0 else "Richtung Stop"
    return ("Klar " if a >= 15 else "Leicht ") + side


# ---------------------------------------------------------------- budget + risk

def size(plan, acct):
    """trader-budget: whole shares inside four limits; says which one decided."""
    entry, risk = plan["entry"], plan["entry"] - plan["stop"]
    cash = acct["account_usd"] * (1 - acct["cash_buffer_pct"] / 100.0)
    limits = {
        "max. Verlust pro Trade": math.floor(acct["max_loss_per_trade_usd"] / risk + 1e-9),
        "Positionslimit": math.floor(acct["account_usd"] * acct["position_cap_pct"] / 100.0 / entry),
        "max. Order": math.floor(acct["max_order_usd"] / entry),
        "Cash nach Puffer": math.floor(cash / entry),
    }
    by = min(limits, key=limits.get)
    shares = max(0, limits[by])
    return {"shares": shares, "limited_by": by, "cost": round(shares * entry, 2),
            "loss_at_stop": round(shares * risk, 2),
            "gain_at_target": round(shares * (plan["target"] - entry), 2)}


def risk_check(plan, sz, rules, acct, today_trades, now_min, market_open):
    """trader-risk: PASS or BLOCK with reasons. Getting out is never blocked."""
    reasons = []
    rr = (plan["target"] - plan["entry"]) / (plan["entry"] - plan["stop"])
    if plan["stop"] >= plan["entry"]:
        reasons.append("Stop liegt nicht unter dem Einstieg")
    if sz["shares"] <= 0:
        reasons.append("Stop zu weit weg für deinen max. Verlust ($%.0f)" % acct["max_loss_per_trade_usd"])
    if sz["loss_at_stop"] > acct["max_loss_per_trade_usd"] + 1e-6:
        reasons.append("Verlust am Stop $%.2f > Limit" % sz["loss_at_stop"])
    if rr + 1e-9 < rules["min_reward_risk"]:
        reasons.append("Chance/Risiko %.2f unter Minimum %.2f" % (rr, rules["min_reward_risk"]))
    n_today = len(today_trades)
    if n_today >= acct["max_trades_per_day"]:
        reasons.append("Schon %d Trades heute (Limit %d)" % (n_today, acct["max_trades_per_day"]))
    used = sum(t.get("loss_at_stop", 0) for t in today_trades)
    if used + sz["loss_at_stop"] > acct["daily_loss_limit_usd"] + 1e-6:
        reasons.append("Tagesrisiko $%.0f + $%.0f > Tageslimit $%.0f" % (used, sz["loss_at_stop"], acct["daily_loss_limit_usd"]))
    if not market_open:
        reasons.append("Markt ist geschlossen")
    elif not (hhmm_to_min(rules["no_entries_before"]) <= now_min <= hhmm_to_min(rules["no_entries_after"])):
        reasons.append("Außerhalb des Einstiegsfensters %s–%s" % (rules["no_entries_before"], rules["no_entries_after"]))
    return {"pass": not reasons, "reasons": reasons, "reward_risk": round(rr, 2)}


# ---------------------------------------------------------------- one call

def analyze(symbol, strategy, today_trades=(), now_ts=None, mc_replays=2000):
    """Everything the chart needs for one ticker, plus the app's own decision."""
    now_ts = now_ts if now_ts is not None else time.time()
    rules, acct = strategy["rules"], strategy["account"]
    params = dict(strategy["params"])
    sym = symbol.upper()
    blocked = strategy.get("blocked_symbols", {}).get(sym)

    data = fetch_bars(sym, "60d", "5m", ttl=55)
    sess = sessions(data["bars"], now_ts)
    if not sess:
        raise EngineError("Keine Handelstage für %s" % sym)
    now = et_now(now_ts)
    now_min = minute_of_day(now)
    today_iso = now.date().isoformat()
    market_open = now.weekday() < 5 and OPEN_MIN <= now_min < CLOSE_MIN and today_iso in sess
    day_key = today_iso if today_iso in sess else list(sess)[-1]
    day = sess[day_key]
    live = day_key == today_iso and market_open
    flat_min = hhmm_to_min(rules["flat_by"])

    hist = backtest(sess, params, rules, skip_date=day_key)
    hist_sum = summarize(hist)

    out = {"symbol": sym, "date": day_key, "live": live, "market_open": market_open,
           "now_et": now.strftime("%Y-%m-%d %H:%M"), "price": day[-1]["c"],
           "params": params, "rules": rules,
           "bars": [[b["t"], round(b["o"], 4), round(b["h"], 4), round(b["l"], 4), round(b["c"], 4), b["v"],
                     round(b["vwap"], 4), round(b["ema"], 4)] for b in day],
           "indicators": {"vwap": round(day[-1]["vwap"], 2), "ema20": round(day[-1]["ema"], 2),
                          "rsi14": round(day[-1]["rsi"], 1) if day[-1].get("rsi") is not None else None,
                          "atr14": round(atr(day), 3) if atr(day) else None,
                          "above_vwap": day[-1]["c"] > day[-1]["vwap"], "above_ema": day[-1]["c"] > day[-1]["ema"]},
           "prev_close": None, "history": hist_sum,
           "history_trades": [{k: t[k] for k in ("date", "outcome", "r", "bars")} for t in hist[-15:]],
           "blocked": blocked, "session_end": None}
    keys = list(sess)
    if keys.index(day_key) > 0:
        out["prev_close"] = sess[keys[keys.index(day_key) - 1]][-1]["c"]
    # session open / flat-by / close as epoch seconds for the timeline
    open_ts = day[0]["t"]
    out["session_open"] = open_ts
    out["flat_ts"] = open_ts + (flat_min - OPEN_MIN) * 60
    out["session_end"] = open_ts + (CLOSE_MIN - OPEN_MIN) * 60

    orr = opening_range(day, params["or_minutes"])
    out["opening_range"] = orr and {"high": round(orr["high"], 2), "low": round(orr["low"], 2), "end": orr["end"]}
    reasons = []
    action, plan, entry_t, sim, walked = "KEIN TRADE", None, None, None, None

    if not orr:
        action = "WARTEN"
        reasons.append("Eröffnungsspanne (%d Min) bildet sich noch" % params["or_minutes"])
        out["decision"] = {"action": action, "reasons": reasons, "state": "forming"}
        return out

    i = find_trigger(day, orr, params, rules)
    if i is not None:
        b = day[i]
        plan = make_plan(b["c"], orr, params)
        entry_t = b["t"] + BAR
        if plan:
            walked = walk(day, i + 1, plan, flat_min)
        state = "triggered"
    else:
        close_min_last = minute_of_day(to_et(day[-1]["t"] + BAR))
        if close_min_last >= hhmm_to_min(rules["no_entries_after"]) or not live:
            state = "expired"
            reasons.append("Kein Ausbruch über %.2f bis %s" % (orr["high"], rules["no_entries_after"]))
        else:
            state = "waiting"
            plan = make_plan(round_cent(orr["high"] + 0.01), orr, params)
            entry_t = day[-1]["t"] + BAR
            dist = (orr["high"] - day[-1]["c"]) / day[-1]["c"] * 100
            reasons.append("Einstieg erst, wenn eine 5-Min-Kerze ÜBER %.2f schließt (%.2f%% entfernt)" % (orr["high"], max(dist, 0)))

    if plan is None and state != "expired":
        reasons.append("Stop zu eng – Spanne zu klein für einen sinnvollen Stop")
        state = "refused"

    if plan:
        start_slot = (minute_of_day(to_et(entry_t)) - OPEN_MIN) // 5
        sim = time_machine(sess, day_key, plan, start_slot, flat_min, replays=mc_replays)
        sz = size(plan, acct)
        rc = risk_check(plan, sz, rules, acct, list(today_trades), now_min, market_open)
        out.update(plan=dict(plan, entry_t=entry_t, end_t=out["flat_ts"]), size=sz, risk=rc, sim=sim)
        if sim:
            sim["verdict"] = verdict(sim["edge_pp"])
            sim["median_exit_t"] = open_ts + sim["median_exit_slot"] * BAR + BAR
        if walked:
            out["result"] = walked

        # the app's own call
        edge_ok = sim is not None and sim["edge_pp"] >= strategy["min_edge_pp"]
        hist_ok = hist_sum["n"] < 8 or (hist_sum["avg_r"] or 0) > 0
        if blocked:
            reasons.append("%s gesperrt: %s" % (sym, blocked))
        if sim is None:
            reasons.append("Zu wenig Historie für die Simulation")
        elif not edge_ok:
            reasons.append("Simulation: %s (Vorteil %+.1f Pp, Minimum %+.1f)" % (sim["verdict"], sim["edge_pp"], strategy["min_edge_pp"]))
        if not hist_ok:
            reasons.append("Regel war auf %s in den letzten %d Ausbrüchen negativ (Ø %+.2fR)" % (sym, hist_sum["n"], hist_sum["avg_r"]))
        good = edge_ok and hist_ok and not blocked
        out["signal_ok"] = bool(good and state == "triggered")
        fresh = entry_t is not None and now_ts - entry_t <= FRESH_SECONDS
        if state == "triggered":
            if walked:
                action = "VORBEI"
                reasons.insert(0, "Trade heute schon beendet: %s (%+.2fR)" % (
                    {"stop": "Stop getroffen", "target": "Ziel erreicht", "flat": "glattgestellt"}[walked["outcome"]], walked["r"]))
            elif good and not fresh:
                action = "LÄUFT"
                reasons.insert(0, "Signal kam um %s bei %.2f, jetzt %.2f: halten, wenn du drin bist, aber nicht hinterherkaufen" % (
                    to_et(entry_t).strftime("%H:%M"), plan["entry"], day[-1]["c"]))
            elif good and rc["pass"]:
                action = "KAUFEN"
                reasons.insert(0, "Ausbruch bestätigt: Kerze schloss %s bei %.2f über %.2f" % (
                    to_et(entry_t).strftime("%H:%M"), plan["entry"], orr["high"]))
            else:
                action = "KEIN TRADE"
                reasons.extend(rc["reasons"])
        elif state == "waiting":
            action = "WARTEN" if good else "BEOBACHTEN"
            if not good:
                reasons.append("Selbst bei Ausbruch spricht die Statistik derzeit dagegen")
        if good and fresh and state == "triggered" and not walked and not rc["pass"]:
            reasons.insert(0, "Signal gut, aber deine Risikoregeln blockieren")
    out["decision"] = {"action": action, "reasons": reasons, "state": state}
    return out
