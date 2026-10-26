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
import threading
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (phil-daytrade-app)"
HOSTS = ("query2", "query1")
BAR = 300  # default bar: 5 minutes. Every bar dict carries its own length in "d" (seconds)
OPEN_MIN = 9 * 60 + 30
CLOSE_MIN = 16 * 60
FULL_BARS = 78  # 5-minute bars in a full session; full_bars() for other lengths
FRESH_SECONDS = 600  # 5-minute bars: a trigger is a buy for 10 minutes; fresh_seconds() scales it
BAR_MINUTES = (1, 2, 5, 15, 30)
STEP = {"1m": 60, "2m": 120, "5m": 300, "15m": 900, "30m": 1800, "60m": 3600, "1d": 86400}


def full_bars(d):
    return (CLOSE_MIN - OPEN_MIN) * 60 // d


def fresh_seconds(d):
    """How long after its bar closes a breakout still counts as a buy: two bars, 5 to 30 minutes."""
    return min(1800, max(300, 2 * d))

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


def parse_iso_ts(text):
    """Epoch seconds of an ISO time like 2026-10-26T18:16:59+00:00, or None."""
    try:
        return int(datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


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
        if rng.startswith("p"):  # "p<start>-<end>": an explicit window in epoch seconds
            start, end = rng[1:].split("-")
            q = "period1=%s&period2=%s" % (start, end)
        else:
            q = "range=%s" % rng
        doc = yahoo("/v8/finance/chart/%s?%s&interval=%s&includePrePost=false" % (sym, q, interval))
        res = (doc.get("chart") or {}).get("result")
        if not res:
            raise EngineError("Keine Daten für %s" % sym)
        res = res[0]
        q = (res.get("indicators") or {}).get("quote", [{}])[0]
        bars = []
        step = STEP.get(interval, BAR)
        for i, t in enumerate(res.get("timestamp") or []):
            o, h, l, c = (q.get(k, [None])[i] for k in ("open", "high", "low", "close"))
            v = (q.get("volume") or [0])[i] or 0
            if None in (o, h, l, c):
                continue  # empty bar
            if step < 86400 and ((minute_of_day(to_et(t)) - OPEN_MIN) * 60) % step:
                continue  # Yahoo's live tick, off the bar grid (bars start at 9:30 New York)
            bars.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": v, "d": step})
        return {"symbol": sym, "meta": res.get("meta", {}), "bars": bars}

    return _cached(("bars", sym, rng, interval), ttl, load)


def history(symbol, minutes=5, ttl=55):
    """Regular bars of the chosen length with as much history as Yahoo keeps.

    1-minute bars come in 7-day pieces (Yahoo's limit per request) for about
    30 calendar days; 2 to 30 minutes come in one 60-day request.
    """
    minutes = int(minutes)
    if minutes not in BAR_MINUTES:
        raise EngineError("Kerzenlänge %s Min wird nicht unterstützt (1, 2, 5, 15 oder 30)" % minutes)
    if minutes != 1:
        return fetch_bars(symbol, "60d", "%dm" % minutes, ttl=ttl)
    sym = symbol.upper().replace(".", "-")

    def load():
        now = int(time.time())
        top = (now // 3600 + 1) * 3600  # whole hours keep the older pieces cacheable
        seen, meta = {}, {}
        for k in range(3, -1, -1):  # four 7-day pieces, oldest first
            end, start = top - k * 7 * 86400, top - (k + 1) * 7 * 86400
            try:
                part = fetch_bars(sym, "p%d-%d" % (start, min(end, now)), "1m", ttl=ttl if k == 0 else 3600)
            except EngineError:
                continue
            meta = part["meta"] or meta
            for b in part["bars"]:
                seen[b["t"]] = b
        if not seen:
            raise EngineError("Keine 1-Minuten-Daten für %s" % sym)
        return {"symbol": sym, "meta": meta, "bars": [seen[t] for t in sorted(seen)]}

    return _cached(("hist1m", sym), ttl, load)


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
        if b["t"] + b["d"] > now_ts:
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
    if not day_bars:
        return None
    d = day_bars[0]["d"]
    n = max(1, -(-or_minutes * 60 // d))  # whole bars covering at least or_minutes
    if len(day_bars) < n or minute_of_day(to_et(day_bars[0]["t"])) != OPEN_MIN:
        return None
    first = day_bars[:n]
    return {"high": max(b["h"] for b in first), "low": min(b["l"] for b in first),
            "avg_vol": sum(b["v"] for b in first) / n, "bars": n, "minutes": n * d // 60,
            "end": day_bars[n - 1]["t"] + d}


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
        close_min = minute_of_day(to_et(b["t"] + b["d"]))
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
        end_t = b["t"] + b["d"]
        end_min = minute_of_day(to_et(end_t))
        if b["l"] <= plan["stop"]:
            return {"outcome": "stop", "r": -1.0, "exit": plan["stop"], "exit_t": end_t, "bars": j - start + 1}
        if b["h"] >= plan["target"]:
            return {"outcome": "target", "r": plan["target_r"], "exit": plan["target"], "exit_t": end_t, "bars": j - start + 1}
        if end_min >= flat_min:
            r = (b["c"] - plan["entry"]) / plan["risk"]
            return {"outcome": "flat", "r": round(r, 3), "exit": b["c"], "exit_t": end_t, "bars": j - start + 1}
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
    return dict(res, entry_t=b["t"] + b["d"], **plan)


def backtest(sess, params, rules, skip_date=None):
    trades = []
    for d, day in sess.items():
        if d == skip_date or len(day) != full_bars(day[0]["d"]):
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

def analyze(symbol, strategy, today_trades=(), now_ts=None, mc_replays=2000, intel=None):
    """Everything the chart needs for one ticker, plus the app's own decision."""
    now_ts = now_ts if now_ts is not None else time.time()
    rules, acct = strategy["rules"], strategy["account"]
    params = dict(strategy["params"])
    sym = symbol.upper()
    blocked = strategy.get("blocked_symbols", {}).get(sym)

    minutes = int(rules.get("bar_minutes", 5))
    data = history(sym, minutes)
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
    bar = day[0]["d"]

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
           "blocked": blocked, "session_end": None, "bar": bar, "bar_minutes": minutes,
           "history_days": sum(1 for dd in sess.values() if len(dd) == full_bars(bar))}
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
    if keys.index(day_key) > 0:
        prev = sess[keys[keys.index(day_key) - 1]]
        out["prev_high"] = round(max(b["h"] for b in prev), 2)
    reasons = []
    action, plan, entry_t, walked, i = "KEIN TRADE", None, None, None, None

    if not orr:
        reasons.append("Eröffnungsspanne (%d Min) bildet sich noch" % max(params["or_minutes"], minutes))
        out["decision"] = {"action": "WARTEN", "reasons": reasons, "state": "forming"}
        return out

    i = find_trigger(day, orr, params, rules)
    if i is not None:
        b = day[i]
        state, entry, entry_t = "triggered", b["c"], b["t"] + b["d"]
        atr_v, vwap_v, rsi_v = atr(day[:i + 1]), b["vwap"], b.get("rsi")
    else:
        close_min_last = minute_of_day(to_et(day[-1]["t"] + bar))
        if close_min_last >= hhmm_to_min(rules["no_entries_after"]) or not live:
            state = "expired"
            reasons.append("Kein Ausbruch über %.2f bis %s" % (orr["high"], rules["no_entries_after"]))
        else:
            state = "waiting"
            entry, entry_t = round_cent(orr["high"] + 0.01), day[-1]["t"] + bar
            atr_v, vwap_v, rsi_v = atr(day), day[-1]["vwap"], day[-1].get("rsi")
            dist = (orr["high"] - day[-1]["c"]) / day[-1]["c"] * 100
            reasons.append("Einstieg erst, wenn eine %d-Min-Kerze ÜBER %.2f schließt (%.2f%% entfernt)" % (minutes, orr["high"], max(dist, 0)))

    opt = None
    if state != "expired":
        import optimize  # here, not at the top: optimize imports this module
        start_slot = (minute_of_day(to_et(entry_t)) - OPEN_MIN) * 60 // bar
        opt = optimize.choose(sess, day_key, entry, orr, atr_v, vwap_v, start_slot, params, rules, acct, replays=mc_replays)
        if opt is None:
            reasons.append("Kein Stop passt: Spanne zu eng oder zu weit, oder zu wenig Historie")
            state = "refused"
        else:
            c = opt["chosen"]
            plan = {k: c[k] for k in ("entry", "stop", "target", "risk", "target_r", "stop_kind")}

    if plan:
        if state == "triggered":
            walked = walk(day, i + 1, plan, flat_min)
        sz = size(plan, acct)
        rc = risk_check(plan, sz, rules, acct, list(today_trades), now_min, market_open)
        c = opt["chosen"]
        be = 1.0 / (1.0 + plan["target_r"])
        decided = c["p_target"] + c["p_stop"]
        share = c["p_target"] / decided if decided else 0.0
        mstep = c["median_exit_step"]
        sim = {"replays": len(opt["paths"]["paths"]), "sessions": opt["paths"]["sessions"],
               "p_target": c["p_target"], "p_stop": c["p_stop"], "p_neither": c["p_neither"],
               "avg_r": c["ev_sim"], "ev_net": c["ev_net"], "ev_hist": c["ev_hist"], "n_hist": c["n_hist"],
               "hist_win": c["hist_win"], "cost_r": c["cost_r"], "ev_usd": round(c["ev_net"] * plan["risk"] * sz["shares"], 2),
               "target_share": round(share, 3), "break_even": round(be, 3), "edge_pp": round((share - be) * 100, 1),
               "median_exit_t": entry_t + (mstep + 1) * bar if mstep is not None else out["flat_ts"]}
        sim["verdict"] = verdict(sim["edge_pp"])
        import optimize
        sim["fan"] = optimize.fan(opt["paths"], plan["entry"])
        out.update(plan=dict(plan, entry_t=entry_t, end_t=out["flat_ts"]), size=sz, risk=rc, sim=sim)
        out["optimizer"] = {"stops": optimize.STOPS, "targets": optimize.TARGETS, "labels": optimize.STOP_LABEL,
                            "chosen": [c["stop_kind"], c["target_r"]],
                            "default": [params["stop"], params["target_r"]],
                            "kept_default": opt["default"] is not None and c is opt["default"],
                            "n_past_breakouts": opt["n_past_breakouts"],
                            "rows": [{k: r.get(k) for k in ("stop_kind", "target_r", "valid", "reachable", "stop", "target", "ev_net",
                                                            "p_target", "p_stop", "n_hist", "ev_hist")} for r in opt["rows"]]}
        if walked:
            out["result"] = walked

        # ---- the checklist: when is it really worth it?
        fresh = state == "triggered" and now_ts - entry_t <= fresh_seconds(bar)
        regime = market_regime(entry_t, now_ts, minutes) if sym != "SPY" else (entry > vwap_v)
        read = (intel or {}).get("read")
        min_ev = strategy.get("min_ev_r", 0.10)
        hist_known = hist_sum["n"] >= 8
        checks = [
            {"key": "signal", "hard": True, "ok": state == "triggered",
             "label": "Ausbruch bestätigt: %d-Min-Kerze schließt über %.2f" % (minutes, orr["high"])},
            {"key": "ev", "hard": True, "ok": c["ev_net"] >= min_ev,
             "label": "Erwartung nach Kosten mindestens %+.2fR" % min_ev,
             "detail": "%+.2fR ≈ %+.2f $ pro Trade" % (c["ev_net"], sim["ev_usd"])},
            {"key": "risk", "hard": True, "ok": rc["pass"], "label": "Deine Risikoregeln",
             "detail": "; ".join(rc["reasons"]) or "PASS"},
            {"key": "blocked", "hard": True, "ok": not blocked, "label": "%s nicht gesperrt" % sym, "detail": blocked or ""},
            {"key": "hist", "hard": False, "ok": ((hist_sum["avg_r"] or 0) > 0) if hist_known else None,
             "label": "Setup auf %s in den letzten 60 Tagen positiv" % sym,
             "detail": ("Ø %+.2fR über %d Ausbrüche" % (hist_sum["avg_r"], hist_sum["n"])) if hist_sum["n"] else "keine Ausbrüche"},
            {"key": "vwap", "hard": False, "ok": entry > vwap_v, "label": "Einstieg über VWAP (%.2f)" % vwap_v},
            {"key": "market", "hard": False, "ok": regime, "label": "Gesamtmarkt (SPY) über seinem VWAP"},
            {"key": "time", "hard": False, "ok": out["flat_ts"] - entry_t >= 5400,
             "label": "Mindestens 90 Min bis zum Glattstellen"},
            {"key": "rsi", "hard": False, "ok": (rsi_v < 75) if rsi_v is not None else None,
             "label": "Nicht überdehnt (RSI 14 unter 75)", "detail": "RSI %.0f" % rsi_v if rsi_v is not None else ""},
            {"key": "liquid", "hard": False, "ok": (read != "against") if read else None,
             "label": "Liquid: Profis halten nicht gegen einen Long"},
        ]
        soft = [x["ok"] for x in checks if not x["hard"] and x["ok"] is not None]
        q = sum(soft) / len(soft) if soft else 0.5
        grade = "A" if q >= 0.8 else "B" if q >= 0.5 else "C"
        grade_ok = grade <= rules.get("min_quality", "B")
        ev_ok = c["ev_net"] >= min_ev
        good = ev_ok and grade_ok and not blocked
        out["checks"], out["quality"] = checks, {"grade": grade, "soft_ok": sum(soft), "soft_known": len(soft),
                                                 "min": rules.get("min_quality", "B")}
        out["signal_ok"] = bool(good and state == "triggered")
        buy_limit = round_cent(plan["entry"] + 0.1 * plan["risk"])
        out["order"] = {"buy_limit": buy_limit, "stop": plan["stop"], "target": plan["target"],
                        "flat_by": rules["flat_by"], "shares": sz["shares"]}

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
                reasons.insert(0, "Jetzt kaufen, Limit höchstens %.2f. Ausbruch um %s bestätigt, Qualität %s, Erwartung %+.2fR" % (
                    buy_limit, to_et(entry_t).strftime("%H:%M"), grade, c["ev_net"]))
            else:
                action = "KEIN TRADE"
        elif state == "waiting":
            action = "WARTEN" if good else "BEOBACHTEN"
            if good:
                reasons.append("Bei Ausbruch handelbar: Qualität %s, Erwartung %+.2fR" % (grade, c["ev_net"]))
        if action in ("KEIN TRADE", "BEOBACHTEN"):
            for x in checks:
                if x["ok"] is False and (x["hard"] or not grade_ok) and x["key"] != "signal":
                    reasons.append("✗ " + x["label"] + (" – " + x["detail"] if x.get("detail") else ""))
            if not grade_ok:
                reasons.append("Qualität %s, verlangt ist mindestens %s (%d von %d Zusatzkriterien erfüllt)" % (
                    grade, rules.get("min_quality", "B"), sum(soft), len(soft)))
    out["decision"] = {"action": action, "reasons": reasons, "state": state}
    return out


def market_regime(at_ts, now_ts, minutes=5):
    """Was SPY above its session VWAP on the last closed bar at or before at_ts?"""
    try:
        sess = sessions(history("SPY", minutes)["bars"], now_ts)
    except EngineError:
        return None
    day = sess.get(to_et(at_ts).date().isoformat())
    if not day:
        return None
    bars = [b for b in day if b["t"] + b["d"] <= at_ts] or day[:1]
    return bars[-1]["c"] > bars[-1]["vwap"]
