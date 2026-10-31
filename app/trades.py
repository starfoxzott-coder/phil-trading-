"""Your own trades: what you really did, how it ended, what it teaches.

A trade you log is stored with a snapshot of the app's plan for that ticker and
day (when there was one), so the two can be compared after the close:

  your R      (exit - entry) / (entry - your stop)
  plan R      what the app's plan did on the same day (its settled signal,
              or a walk of the plan through the day's bars)
  deviations  read from the numbers, no guessing: bought above the limit,
              bought before the candle closed, wider stop, sold before the
              plan's target or stop, held past flat-by, position too big

Your verdict ("war gut" / "war schlecht"), your mistake tags and your note are
stored as you give them. A mistake that keeps costing you becomes a personal
rule the app shows next to every order. Screenshots live in data/shots/.
"""

import base64
import datetime
import json
import os
import re
import time

import engine

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
FILE = os.path.join(DATA, "my-trades.jsonl")
SHOTS = os.path.join(DATA, "shots")

IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024

MISTAKES = {
    "chased": "Nachgejagt (über dem Kauf-Limit gekauft)",
    "early_entry": "Vor dem Kerzenschluss gekauft",
    "wider_stop": "Stop weiter als im Plan",
    "moved_stop": "Stop während des Trades verschoben",
    "early_exit": "Zu früh verkauft",
    "late_exit": "Über die Glattstellzeit gehalten",
    "too_big": "Position größer als dein Verlustlimit",
    "no_plan": "Ohne Signal der App gehandelt",
    "against_app": "Gehandelt, obwohl die App abgeraten hat",
    "fomo": "Aus Angst, etwas zu verpassen",
    "revenge": "Verlust zurückholen wollen",
}
RULE_TEXT = {
    "chased": "Kaufe nur bis zum Kauf-Limit. Liegt der Kurs darüber, lass den Trade aus.",
    "early_entry": "Warte, bis die Kerze wirklich über dem Spannenhoch geschlossen hat.",
    "wider_stop": "Setze den Stop genau dort, wo der Plan ihn hat, nicht tiefer.",
    "moved_stop": "Verschiebe den Stop nach dem Kauf nie nach unten.",
    "early_exit": "Lass den Trade bis Ziel, Stop oder Glattstellzeit laufen. Nicht vorher verkaufen.",
    "late_exit": "Verkaufe spätestens zur Glattstellzeit, auch im Gewinn.",
    "too_big": "Kaufe nur die Stückzahl, die die App angibt.",
    "no_plan": "Handle nur Signale mit KAUFEN.",
    "against_app": "Sagt die App KEIN TRADE oder BEOBACHTEN, dann lass es.",
    "fomo": "Kein Signal, kein Trade, auch wenn die Aktie davonläuft.",
    "revenge": "Nach einem Verlust die nächste Gelegenheit abwarten, nicht sofort wieder rein.",
}
RULE_MIN = 3  # occurrences before a mistake becomes a personal rule


def _read():
    if not os.path.exists(FILE):
        return []
    with open(FILE, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _write(rows):
    os.makedirs(DATA, exist_ok=True)
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, FILE)


def all_trades():
    return _read()


def get(trade_id):
    return next((t for t in _read() if t["id"] == trade_id), None)


def _num(v, what, positive=True):
    try:
        x = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError("%s: bitte eine Zahl" % what)
    if positive and x <= 0:
        raise ValueError("%s muss größer als 0 sein" % what)
    return x


def et_ts(date_iso, hhmm):
    """Epoch seconds of a New York wall-clock time on a date."""
    local = datetime.datetime.combine(datetime.date.fromisoformat(date_iso), datetime.time())
    local += datetime.timedelta(minutes=engine.hhmm_to_min(hhmm))
    guess = local + datetime.timedelta(hours=5)
    off = engine.et_offset_hours(guess)
    return int((local - datetime.timedelta(hours=off)).replace(tzinfo=datetime.timezone.utc).timestamp())


def save_image(trade_id, data_url, tag):
    m = re.match(r"^data:(image/[a-z]+);base64,(.+)$", data_url or "", re.S)
    if not m or m.group(1) not in IMAGE_TYPES:
        raise ValueError("Bild: nur PNG, JPG, WEBP oder GIF")
    raw = base64.b64decode(m.group(2), validate=False)
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("Bild zu groß (max. 8 MB)")
    os.makedirs(SHOTS, exist_ok=True)
    name = "%s-%s-%d.%s" % (trade_id, tag, int(time.time() * 1000) % 100000, IMAGE_TYPES[m.group(1)])
    with open(os.path.join(SHOTS, name), "wb") as f:
        f.write(raw)
    return name


def shot_path(name):
    """Path of a stored screenshot, or None for anything that is not one."""
    if not re.match(r"^[A-Za-z0-9_.-]+\.(png|jpg|webp|gif)$", name or ""):
        return None
    p = os.path.join(SHOTS, name)
    return p if os.path.isfile(p) else None


# ---------------------------------------------------------------- open a trade

def open_trade(body, analysis=None):
    """Log a trade you made. `analysis` is the app's view of that ticker today, if any."""
    sym = str(body.get("symbol", "")).strip().upper()
    if not re.match(r"^[A-Z0-9.-]{1,10}$", sym):
        raise ValueError("Ticker fehlt")
    entry = _num(body.get("entry"), "Kaufkurs")
    shares = int(_num(body.get("shares"), "Stück"))
    stop = _num(body.get("stop"), "Stop") if body.get("stop") not in (None, "") else None
    target = _num(body.get("target"), "Ziel") if body.get("target") not in (None, "") else None
    if stop is not None and stop >= entry:
        raise ValueError("Der Stop muss unter dem Kaufkurs liegen (nur Long)")
    date = body.get("date") or engine.et_now().date().isoformat()
    hhmm = body.get("time") or engine.et_now().strftime("%H:%M")
    tid = "T%s-%s-%s" % (date.replace("-", ""), hhmm.replace(":", ""), sym)
    rows = _read()
    if any(t["id"] == tid for t in rows):
        tid += "-%d" % (len(rows) + 1)
    t = {"id": tid, "symbol": sym, "date": date, "time": hhmm, "entry_ts": et_ts(date, hhmm),
         "entry": round(entry, 4), "shares": shares, "stop": stop, "target": target,
         "note": str(body.get("note") or "")[:2000], "status": "open", "images": [], "created_at": int(time.time())}
    if analysis and analysis.get("plan") and analysis.get("date") == date:
        p = analysis["plan"]
        t["plan"] = {"entry": p["entry"], "stop": p["stop"], "target": p["target"], "target_r": p["target_r"],
                     "stop_kind": p.get("stop_kind"), "entry_t": p["entry_t"], "end_t": p["end_t"],
                     "buy_limit": (analysis.get("order") or {}).get("buy_limit"),
                     "shares": (analysis.get("size") or {}).get("shares"),
                     "action": analysis["decision"]["action"], "grade": (analysis.get("quality") or {}).get("grade"),
                     "bar_minutes": analysis.get("bar_minutes", 5),
                     "ev_r": (analysis.get("sim") or {}).get("ev_net")}
        t["signal_id"] = (analysis.get("signal") or {}).get("id")
    risk = entry - (stop if stop is not None else (t.get("plan") or {}).get("stop", entry * 0.99))
    t["loss_at_stop"] = round(max(risk, 0) * shares, 2)
    if body.get("image"):
        t["images"].append({"name": save_image(tid, body["image"], "entry"), "when": "entry"})
    elif body.get("image_name") and shot_path(body["image_name"]):  # already uploaded for "read screenshot"
        t["images"].append({"name": body["image_name"], "when": "entry"})
    rows.append(t)
    _write(rows)
    return t


def add_image(trade_id, data_url, when="extra"):
    rows = _read()
    t = next((x for x in rows if x["id"] == trade_id), None)
    if not t:
        raise ValueError("Trade nicht gefunden")
    t["images"].append({"name": save_image(trade_id, data_url, when), "when": when})
    _write(rows)
    return t


def delete(trade_id):
    rows = _read()
    t = next((x for x in rows if x["id"] == trade_id), None)
    if not t:
        raise ValueError("Trade nicht gefunden")
    for im in t.get("images", []):
        p = shot_path(im["name"])
        if p:
            os.remove(p)
    _write([x for x in rows if x["id"] != trade_id])


# ---------------------------------------------------------------- close a trade

def plan_r(t, rules):
    """What the app's plan did on that day, from the day's own bars."""
    p = t.get("plan")
    if not p:
        return None
    try:
        sess = engine.sessions(engine.history(t["symbol"], p.get("bar_minutes", 5))["bars"])
    except engine.EngineError:
        return None
    day = sess.get(t["date"])
    if not day:
        return None
    start = next((k for k, b in enumerate(day) if b["t"] >= p["entry_t"]), None)
    if start is None:
        return None
    plan = dict(p, risk=p["entry"] - p["stop"])
    res = engine.walk(day, start, plan, engine.hhmm_to_min(rules["flat_by"]))
    return res and {"r": res["r"], "outcome": res["outcome"], "exit": res["exit"], "exit_t": res["exit_t"]}


def deviations(t, rules, acct):
    """Mistakes the numbers prove. Each one names its evidence."""
    out = []
    p = t.get("plan")
    if p:
        if p.get("action") in ("KEIN TRADE", "BEOBACHTEN", "VORBEI", "LÄUFT"):
            out.append(("against_app", "App sagte %s%s" % (p["action"], " (nicht mehr nachkaufen)" if p["action"] == "LÄUFT" else "")))
        if p.get("buy_limit") and t["entry"] > p["buy_limit"] + 0.005:
            out.append(("chased", "Kauf %.2f, Limit war %.2f" % (t["entry"], p["buy_limit"])))
        if p.get("action") in ("WARTEN", "BEOBACHTEN"):
            out.append(("early_entry", "Kauf %s, der Ausbruch über das Spannenhoch war noch nicht bestätigt" % t["time"]))
        elif t["entry_ts"] < p["entry_t"] - 60:
            out.append(("early_entry", "Kauf %s, Kerzenschluss erst %s ET" % (
                t["time"], engine.to_et(p["entry_t"]).strftime("%H:%M"))))
        if t.get("stop") is not None and t["stop"] < p["stop"] - 0.005:
            out.append(("wider_stop", "Stop %.2f statt %.2f" % (t["stop"], p["stop"])))
        pr = t.get("plan_result")
        if pr and t.get("exit_reason") == "manual" and pr["r"] > (t.get("r") or 0) + 0.25:
            out.append(("early_exit", "Plan hätte %+.2fR gebracht, du %+.2fR" % (pr["r"], t.get("r") or 0)))
    if t.get("exit_ts") and t["exit_ts"] > et_ts(t["date"], rules["flat_by"]) + 120:
        out.append(("late_exit", "Verkauf %s, Glattstellen bis %s" % (t.get("exit_time"), rules["flat_by"])))
    if t.get("loss_at_stop", 0) > acct["max_loss_per_trade_usd"] * 1.05:
        out.append(("too_big", "Risiko $%.0f, Limit $%.0f" % (t["loss_at_stop"], acct["max_loss_per_trade_usd"])))
    return out


def close_trade(trade_id, body, strategy):
    rows = _read()
    t = next((x for x in rows if x["id"] == trade_id), None)
    if not t:
        raise ValueError("Trade nicht gefunden")
    rules, acct = strategy["rules"], strategy["account"]
    if body.get("auto"):
        res = auto_exit(t, rules)
        if not res:
            raise ValueError("Für diesen Tag gibt es noch nicht genug Kursdaten")
        exit_px, exit_ts, reason = res["exit"], res["exit_t"], {"stop": "stop", "target": "target", "flat": "flat"}[res["outcome"]]
        t["auto_settled"] = True
    else:
        exit_px = _num(body.get("exit"), "Verkaufskurs")
        exit_ts = et_ts(t["date"], body.get("exit_time") or engine.et_now().strftime("%H:%M"))
        reason = body.get("exit_reason") if body.get("exit_reason") in ("target", "stop", "manual", "flat") else "manual"
    risk = t["entry"] - (t["stop"] if t.get("stop") is not None else (t.get("plan") or {}).get("stop", t["entry"] * 0.99))
    t.update(status="closed", exit=round(exit_px, 4), exit_ts=exit_ts,
             exit_time=engine.to_et(exit_ts).strftime("%H:%M"), exit_reason=reason,
             r=round((exit_px - t["entry"]) / risk, 3) if risk > 0 else None,
             pnl=round((exit_px - t["entry"]) * t["shares"], 2))
    if body.get("stop_moved"):
        t["stop_moved"] = True
    t["plan_result"] = plan_r(t, rules)
    auto = deviations(t, rules, acct)
    tags = [m for m in (body.get("mistakes") or []) if m in MISTAKES]
    if t.get("stop_moved") and "moved_stop" not in tags:
        tags.append("moved_stop")
    if not t.get("plan") and "no_plan" not in tags:
        tags.append("no_plan")
    t["auto_mistakes"] = [{"key": k, "evidence": e} for k, e in auto]
    t["mistakes"] = sorted(set(tags) | {k for k, _ in auto})
    t["followed_plan"] = bool(t.get("plan")) and not t["mistakes"]
    if body.get("rating") in ("good", "bad", "neutral"):
        t["rating"] = body["rating"]
    if body.get("note_after") is not None:
        t["note_after"] = str(body["note_after"])[:2000]
    if body.get("image"):
        t["images"].append({"name": save_image(trade_id, body["image"], "exit"), "when": "exit"})
    _write(rows)
    return t


def auto_exit(t, rules):
    """Your stop and target walked through the day's bars, for a trade you did not close by hand."""
    try:  # 1-minute bars where Yahoo still has them: the closest look at what happened
        sess = engine.sessions(engine.history(t["symbol"], 1)["bars"])
        if t["date"] not in sess:
            sess = engine.sessions(engine.history(t["symbol"], 5)["bars"])
    except engine.EngineError:
        return None
    day = sess.get(t["date"])
    if not day:
        return None
    stop = t.get("stop") or (t.get("plan") or {}).get("stop")
    target = t.get("target") or (t.get("plan") or {}).get("target")
    if not stop or not target:
        return None
    start = next((k for k, b in enumerate(day) if b["t"] + b["d"] > t["entry_ts"]), None)
    if start is None:
        return None
    risk = t["entry"] - stop
    plan = {"entry": t["entry"], "stop": stop, "target": target, "risk": risk, "target_r": (target - t["entry"]) / risk}
    return engine.walk(day, start, plan, engine.hhmm_to_min(rules["flat_by"]))


def rate(trade_id, rating, note=None):
    rows = _read()
    t = next((x for x in rows if x["id"] == trade_id), None)
    if not t:
        raise ValueError("Trade nicht gefunden")
    if rating not in ("good", "bad", "neutral"):
        raise ValueError("Bewertung: good, bad oder neutral")
    t["rating"] = rating
    if note is not None:
        t["note_after"] = str(note)[:2000]
    _write(rows)
    return t


def set_review(trade_id, review):
    """Store Claude's review; its mistake tags join yours and the ones the numbers prove."""
    rows = _read()
    t = next((x for x in rows if x["id"] == trade_id), None)
    if t:
        t["review"] = review
        if t["status"] == "closed":
            t["mistakes"] = sorted(set(t.get("mistakes") or []) | {m for m in review.get("mistakes", []) if m in MISTAKES})
            t["followed_plan"] = bool(t.get("plan")) and not t["mistakes"]
        _write(rows)
    return t


def settle_open(strategy, before_date):
    """Close trades from past days you never closed, from your own stop and target (marked as estimated)."""
    done = []
    for t in _read():
        if t["status"] == "open" and t["date"] < before_date:
            try:
                done.append(close_trade(t["id"], {"auto": True}, strategy))
            except ValueError:
                pass
    return done


# ---------------------------------------------------------------- what it teaches

def today_risk(date_iso):
    """Your logged trades of a day, as the risk check counts them."""
    return [{"loss_at_stop": t.get("loss_at_stop", 0), "signal_id": t.get("signal_id")}
            for t in _read() if t["date"] == date_iso]


def _stats(rows):
    if not rows:
        return {"n": 0, "avg_r": None, "win_rate": None}
    return {"n": len(rows), "avg_r": round(sum(t["r"] for t in rows) / len(rows), 3),
            "win_rate": round(sum(1 for t in rows if t["r"] > 0) / len(rows), 3)}


def summary():
    done = [t for t in _read() if t["status"] == "closed" and t.get("r") is not None]
    out = {"n": len(done), "open": sum(1 for t in _read() if t["status"] == "open")}
    if not done:
        return dict(out, rules=[], mistakes=[])
    out["avg_r"] = round(sum(t["r"] for t in done) / len(done), 3)
    out["win_rate"] = round(sum(1 for t in done if t["r"] > 0) / len(done), 3)
    out["pnl"] = round(sum(t.get("pnl", 0) for t in done), 2)
    both = [t for t in done if t.get("plan_result")]
    if both:
        out["vs_plan"] = {"n": len(both), "you": round(sum(t["r"] for t in both) / len(both), 3),
                          "plan": round(sum(t["plan_result"]["r"] for t in both) / len(both), 3)}
    followed = [t for t in done if t.get("followed_plan")]
    broke = [t for t in done if t.get("plan") and not t.get("followed_plan")]
    if followed or broke:
        out["discipline"] = {"followed": _stats(followed), "broke": _stats(broke)}
    for k in ("good", "bad"):
        g = [t for t in done if t.get("rating") == k]
        if g:
            out["rated_" + k] = _stats(g)
    ms = []
    for key, label in MISTAKES.items():
        hit = [t for t in done if key in (t.get("mistakes") or [])]
        if not hit:
            continue
        cost = [t["r"] - t["plan_result"]["r"] for t in hit if t.get("plan_result")]
        ms.append({"key": key, "label": label, "n": len(hit), "avg_r": round(sum(t["r"] for t in hit) / len(hit), 3),
                   "cost_vs_plan": round(sum(cost) / len(cost), 3) if cost else None})
    ms.sort(key=lambda m: -m["n"])
    out["mistakes"] = ms
    out["rules"] = personal_rules(ms)
    return out


def personal_rules(ms):
    """A mistake you made at least RULE_MIN times that cost you becomes a rule."""
    rules = []
    for m in ms:
        costly = (m["cost_vs_plan"] is not None and m["cost_vs_plan"] < -0.1) or m["avg_r"] < 0
        if m["n"] >= RULE_MIN and costly:
            why = "%d× passiert, Ø %+.2fR" % (m["n"], m["avg_r"])
            if m["cost_vs_plan"] is not None:
                c = m["cost_vs_plan"]
                why += ", %.2fR %s als der Plan" % (abs(c), "schlechter" if c < 0 else "besser")
            rules.append({"key": m["key"], "text": RULE_TEXT[m["key"]], "why": why})
    return rules


def for_day(symbol, date_iso):
    return [t for t in _read() if t["symbol"] == symbol.upper() and t["date"] == date_iso]
